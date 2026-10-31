"""SQLite store behind a small repository layer.

The repository interface is the seam for the Postgres target: a future
PostgresStore would implement the same methods and be selected from
DATABASE_URL, with no endpoint code changing.

DATABASE_URL handling (current backend: SQLite):
  - unset            -> ./agentdocs.db next to the working directory
  - sqlite:///path   -> that file
  - sqlite:///:memory: -> in-memory (tests)
  - a plain path     -> that file
  - postgresql://... -> RuntimeError: Postgres backend is not implemented
    yet; SQLite is the current backend. This is deliberate: the store
    must not silently pretend a Postgres URL is being honoured.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass


def _sqlite_path_from_database_url(database_url: str | None) -> str:
    if not database_url:
        return "agentdocs.db"
    if database_url.startswith("postgresql://") or database_url.startswith(
        "postgres://"
    ):
        raise RuntimeError(
            "Postgres backend is not implemented yet; "
            "SQLite is the current backend (see README)."
        )
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    if database_url.startswith("sqlite://"):
        return database_url[len("sqlite://") :]
    return database_url


@dataclass
class Document:
    id: str
    title: str
    actor: str
    created_at: str


@dataclass
class Version:
    id: str
    document_id: str
    number: int
    sha256: str
    change_summary: str
    html: str
    base_version_id: str | None
    actor: str
    created_at: str


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Store:
    """SQLite-backed repository. All SQL lives here, nowhere else."""

    def __init__(self, database_url: str | None = None) -> None:
        self.path = _sqlite_path_from_database_url(database_url)
        # check_same_thread=False: FastAPI may serve from multiple threads;
        # each method opens its own short-lived connection.
        self._connect().close()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    number INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    change_summary TEXT NOT NULL DEFAULT '',
                    html TEXT NOT NULL,
                    base_version_id TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (document_id, number)
                );
                """
            )

    # -- documents -----------------------------------------------------
    def create_document(
        self, title: str, html: str, change_summary: str, actor: str
    ) -> tuple[Document, Version]:
        doc_id = _new_id("doc")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO documents (id, title, actor) VALUES (?, ?, ?)",
                (doc_id, title, actor),
            )
        version = self._insert_version(
            doc_id, 1, html, change_summary, None, actor
        )
        doc = self.get_document(doc_id)
        assert doc is not None
        return doc, version

    def get_document(self, doc_id: str) -> Document | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        if row is None:
            return None
        return Document(row["id"], row["title"], row["actor"], row["created_at"])

    def list_documents(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.title, d.created_at,
                       (SELECT MAX(number) FROM versions v
                         WHERE v.document_id = d.id) AS latest_version
                FROM documents d ORDER BY d.created_at DESC, d.id DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # -- versions ------------------------------------------------------
    def _insert_version(
        self,
        doc_id: str,
        number: int,
        html: str,
        change_summary: str,
        base_version_id: str | None,
        actor: str,
    ) -> Version:
        version_id = _new_id("ver")
        sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO versions
                  (id, document_id, number, sha256, change_summary,
                   html, base_version_id, actor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    doc_id,
                    number,
                    sha,
                    change_summary,
                    html,
                    base_version_id,
                    actor,
                ),
            )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_version(row)

    def latest_version(self, doc_id: str) -> Version | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM versions WHERE document_id = ?
                ORDER BY number DESC LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def publish_version(
        self, doc_id: str, html: str, base_version_id: str,
        change_summary: str, actor: str,
    ) -> tuple[Version | None, str | None]:
        """Return (version, current_version_id).

        If base_version_id is stale, version is None and
        current_version_id is the latest version id (409 case).
        """
        latest = self.latest_version(doc_id)
        if latest is None:
            return None, None
        if latest.id != base_version_id:
            return None, latest.id
        return (
            self._insert_version(
                doc_id, latest.number + 1, html, change_summary,
                base_version_id, actor,
            ),
            None,
        )

    def list_versions(self, doc_id: str) -> list[Version]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM versions WHERE document_id = ?
                ORDER BY number ASC
                """,
                (doc_id,),
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, doc_id: str, version_id: str) -> Version | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM versions
                WHERE document_id = ? AND id = ?
                """,
                (doc_id, version_id),
            ).fetchone()
        return self._row_to_version(row) if row else None

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> Version:
        return Version(
            row["id"], row["document_id"], row["number"], row["sha256"],
            row["change_summary"], row["html"], row["base_version_id"],
            row["actor"], row["created_at"],
        )


def get_store() -> Store:
    """Production store, selected from DATABASE_URL (SQLite now)."""
    return Store(os.environ.get("DATABASE_URL"))
