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
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass

# API registry: app short name -> (api_url, token_url, scope,
# swagger_url, endpoints). The app short name (e.g. "refdp") is
# the registry key; endpoints are paths from its Swagger doc.
# Names are slugs; URLs must be https (http only for localhost
# dev). The registry stores URLs only — never tokens or secrets.
# A token URL is fetched at view time, per viewer, per endpoint.
# swagger_url is stored in the app config so a later sync can
# fetch the latest Swagger without the caller re-supplying it.
_API_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def validate_api_registration(name: str, api_url: str, token_url: str) -> None:
    if not _API_NAME_RE.match(name):
        raise ValueError("invalid_api_name")
    for url in (api_url, token_url):
        if not (
            url.startswith("https://")
            or url.startswith("http://127.0.0.1")
            or url.startswith("http://localhost")
        ):
            raise ValueError("api_url_must_be_https")


# Shared document state (KV), SlashDocs-style rules:
# keys: letters/numbers/dot/underscore/colon/dash, max 128 chars;
# values: JSON-serializable, max 64 KiB when dumped; null = delete;
# a patch holds at most 256 keys. State is document-scoped, NOT
# version-scoped, and changing it never creates a version.
_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_MAX_VALUE_BYTES = 64 * 1024
_MAX_PATCH_KEYS = 256


def validate_state_key(key: str) -> None:
    if not _KEY_RE.match(key):
        raise ValueError("invalid_state_key")


def validate_state_value(value) -> None:
    try:
        dumped = json.dumps(value)
    except (TypeError, ValueError):
        raise ValueError("state_value_not_json")
    if len(dumped.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise ValueError("state_value_too_large")


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
                CREATE TABLE IF NOT EXISTS document_kv (
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (document_id, key)
                );
                CREATE TABLE IF NOT EXISTS apis (
                    name TEXT PRIMARY KEY,
                    api_url TEXT NOT NULL,
                    token_url TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            # Lightweight migration for DBs created before app/swagger
            # columns existed: add them if missing, keep existing rows.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(apis)").fetchall()}
            for col, ddl in [
                ("app", "TEXT NOT NULL DEFAULT ''"),
                ("swagger_url", "TEXT NOT NULL DEFAULT ''"),
                ("endpoints", "TEXT NOT NULL DEFAULT '[]'"),
            ]:
                if col not in cols:
                    conn.execute(f"ALTER TABLE apis ADD COLUMN {col} {ddl}")
            conn.execute("UPDATE apis SET app = name WHERE app = ''")

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

    # -- shared document state (KV) ----------------------------------
    def get_state(self, doc_id: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM document_kv WHERE document_id = ? ORDER BY key",
                (doc_id,),
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def get_state_value(self, doc_id: str, key: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM document_kv WHERE document_id = ? AND key = ?",
                (doc_id, key),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state_value(self, doc_id: str, key: str, value, actor: str) -> bool:
        """Set one key; a null value deletes it. Returns True if a key remains."""
        validate_state_key(key)
        if value is None:
            # Null deletes; the key does NOT remain, so return False.
            self.delete_state_value(doc_id, key)
            return False
        validate_state_value(value)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO document_kv (document_id, key, value, actor, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(document_id, key) DO UPDATE SET
                    value = excluded.value, actor = excluded.actor,
                    updated_at = datetime('now')
                """,
                (doc_id, key, json.dumps(value), actor),
            )
        return True

    def delete_state_value(self, doc_id: str, key: str) -> bool:
        validate_state_key(key)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM document_kv WHERE document_id = ? AND key = ?",
                (doc_id, key),
            )
            # Capture inside the block: rowcount is lost once the
            # connection context closes the cursor.
            deleted = cur.rowcount > 0
        return deleted

    def patch_state(self, doc_id: str, patch: dict, actor: str) -> dict:
        if len(patch) > _MAX_PATCH_KEYS:
            raise ValueError("state_patch_too_large")
        for key, value in patch.items():
            self.set_state_value(doc_id, key, value, actor)
        return self.get_state(doc_id)

    # -- API registry --------------------------------------------------
    def register_api(self, name: str, api_url: str, token_url: str, scope: str, actor: str, app: str | None = None, swagger_url: str | None = None) -> dict | None:
        validate_api_registration(name, api_url, token_url)
        if app is not None and not _API_NAME_RE.match(app):
            raise ValueError("invalid_api_name")
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO apis (name, api_url, token_url, scope, actor, app, swagger_url, endpoints) VALUES (?, ?, ?, ?, ?, ?, ?, '[]')",
                    (name, api_url, token_url, scope or "", actor, app or name, swagger_url or ""),
                )
        except sqlite3.IntegrityError:
            return None
        return self.get_api(name)

    def list_apis(self, app: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if app:
                rows = conn.execute("SELECT * FROM apis WHERE app = ? ORDER BY name", (app,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM apis ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_api(self, name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM apis WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def update_api(self, name: str, api_url: str | None, token_url: str | None, scope: str | None, app: str | None = None, swagger_url: str | None = None, endpoints: list[str] | None = None) -> dict | None:
        existing = self.get_api(name)
        if existing is None:
            return None
        new_api_url = api_url if api_url is not None else existing["api_url"]
        new_token_url = token_url if token_url is not None else existing["token_url"]
        validate_api_registration(name, new_api_url, new_token_url)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE apis SET api_url = ?, token_url = ?, scope = ?, app = ?, swagger_url = ?, endpoints = ?, updated_at = datetime('now')
                WHERE name = ?
                """,
                (
                    new_api_url,
                    new_token_url,
                    scope if scope is not None else existing["scope"],
                    app if app is not None else existing.get("app") or name,
                    swagger_url if swagger_url is not None else existing.get("swagger_url") or "",
                    json.dumps(endpoints) if endpoints is not None else existing.get("endpoints") or "[]",
                    name,
                ),
            )
        return self.get_api(name)

    def delete_api(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM apis WHERE name = ?", (name,))
            deleted = cur.rowcount > 0
        return deleted

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
