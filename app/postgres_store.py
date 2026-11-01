"""Postgres store for agent-docs: same repository interface as the
SQLite Store, selected when DATABASE_URL starts with
postgresql:// or postgres:// (see store.get_store).

The schema lives in postgres_schema.sql next to this file and is
run on init; it is idempotent, so it also migrates databases
created before drafts / app grouping / user_id existed.

Connection handling: one short-lived psycopg connection per
method, same pattern as the SQLite store's per-method connection.
psycopg is an optional dependency — install it with
``pip install agent-docs-api[postgres]`` (or ``psycopg[binary]``).
The service account in DATABASE_URL is the already-governed DB
service account; no credential is ever minted for an agent or a
user, and no credential appears in this file.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .store import (
    _API_NAME_RE,
    _MAX_PATCH_KEYS,
    _new_id,
    Document,
    Version,
    validate_api_registration,
    validate_state_key,
    validate_state_value,
)


def _text(value):
    """Timestamps come back as datetime; the API shape is text."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(row) -> dict:
    return {k: _text(v) for k, v in dict(row).items()}


def _row_to_version(row) -> Version:
    return Version(
        row["id"],
        row["document_id"],
        row["number"],
        row["sha256"],
        row["change_summary"],
        row["html"],
        row["base_version_id"],
        row["actor"],
        _text(row["created_at"]),
        s3_key=row.get("s3_key") or "",
    )


class PostgresStore:
    """Postgres-backed repository. All SQL lives here, nowhere else."""

    def __init__(self, database_url: str, blob_store=None):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover - missing optional dep
            raise RuntimeError(
                "Postgres backend needs psycopg: "
                "pip install agent-docs-api[postgres]"
            ) from e
        self._psycopg = psycopg
        # Same blob rule as the SQLite store: when set, published
        # HTML goes to the blob store and the row holds only s3_key.
        self.blob_store = blob_store
        self.dsn = database_url
        self._init_schema()

    def _connect(self):
        # dict_row: rows behave like dicts, matching the SQLite store.
        return self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row)

    def _init_schema(self) -> None:
        schema = (Path(__file__).parent / "postgres_schema.sql").read_text(encoding="utf-8")
        # psycopg's execute() is single-statement; split the script
        # on statement terminators instead of sending it as one query,
        # stripping comment lines from each chunk (the file's header
        # comment sits in the same chunk as the first CREATE TABLE).
        statements = []
        for chunk in schema.split(";\n"):
            lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
            stmt = "\n".join(lines).strip()
            if stmt:
                statements.append(stmt)
        with self._connect() as conn:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)

    # -- documents -----------------------------------------------------
    def create_document(
        self, title: str, html: str, change_summary: str, actor: str
    ) -> tuple[Document, Version]:
        doc_id = _new_id("doc")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents (id, title, actor) VALUES (%s, %s, %s)",
                    (doc_id, title, actor),
                )
        version = self._insert_version(doc_id, 1, html, change_summary, None, actor)
        doc = self.get_document(doc_id)
        assert doc is not None
        return doc, version

    def get_document(self, doc_id: str) -> Document | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM documents WHERE id = %s", (doc_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return Document(row["id"], row["title"], row["actor"], _text(row["created_at"]))

    def list_documents(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id, d.title, d.created_at::text AS created_at,
                           (SELECT MAX(number) FROM versions v
                             WHERE v.document_id = d.id) AS latest_version,
                           (d.draft_html <> '') AS has_draft
                    FROM documents d ORDER BY d.created_at DESC, d.id DESC
                    """
                )
                rows = cur.fetchall()
        return [_row_dict(r) for r in rows]

    # -- drafts (same semantics as the SQLite store) -------------------
    def create_draft_document(self, title: str, html: str, actor: str) -> Document:
        doc_id = _new_id("doc")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents (id, title, actor, draft_html, draft_updated_at) "
                    "VALUES (%s, %s, %s, %s, now())",
                    (doc_id, title, actor, html),
                )
        doc = self.get_document(doc_id)
        assert doc is not None
        return doc

    def set_draft(self, doc_id: str, html: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET draft_html = %s, draft_updated_at = now() WHERE id = %s",
                    (html, doc_id),
                )
                return cur.rowcount > 0

    def get_draft(self, doc_id: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT draft_html FROM documents WHERE id = %s", (doc_id,))
                row = cur.fetchone()
        if row is None or not row["draft_html"]:
            return None
        return row["draft_html"]

    def has_draft(self, doc_id: str) -> bool:
        return self.get_draft(doc_id) is not None

    def delete_draft(self, doc_id: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET draft_html = '', draft_updated_at = NULL "
                    "WHERE id = %s AND draft_html <> ''",
                    (doc_id,),
                )
                return cur.rowcount > 0

    def publish_draft(self, doc_id: str, change_summary: str, actor: str) -> Version | None:
        draft = self.get_draft(doc_id)
        if draft is None:
            return None
        latest = self.latest_version(doc_id)
        number = latest.number + 1 if latest else 1
        base = latest.id if latest else None
        version = self._insert_version(doc_id, number, draft, change_summary, base, actor)
        self.delete_draft(doc_id)
        return version

    # -- versions ------------------------------------------------------
    def _insert_version(
        self, doc_id: str, number: int, html: str, change_summary: str,
        base_version_id: str | None, actor: str,
    ) -> Version:
        version_id = _new_id("ver")
        sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
        # Upload first, pointer second, same as the SQLite store.
        s3_key = ""
        stored_html = html
        if self.blob_store is not None:
            from .blob_store import version_blob_key

            s3_key = version_blob_key(doc_id, version_id)
            self.blob_store.put(s3_key, html.encode("utf-8"))
            stored_html = ""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO versions
                      (id, document_id, number, sha256, change_summary,
                       html, s3_key, base_version_id, actor)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (version_id, doc_id, number, sha, change_summary, stored_html, s3_key, base_version_id, actor),
                )
                cur.execute("SELECT * FROM versions WHERE id = %s", (version_id,))
                row = cur.fetchone()
        assert row is not None
        return _row_to_version(row)

    def latest_version(self, doc_id: str) -> Version | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM versions WHERE document_id = %s ORDER BY number DESC LIMIT 1",
                    (doc_id,),
                )
                row = cur.fetchone()
        return _row_to_version(row) if row else None

    def publish_version(
        self, doc_id: str, html: str, base_version_id: str,
        change_summary: str, actor: str,
    ) -> tuple[Version | None, str | None]:
        latest = self.latest_version(doc_id)
        if latest is None:
            return None, None
        if latest.id != base_version_id:
            return None, latest.id
        return (
            self._insert_version(doc_id, latest.number + 1, html, change_summary, base_version_id, actor),
            None,
        )

    def list_versions(self, doc_id: str) -> list[Version]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM versions WHERE document_id = %s ORDER BY number ASC",
                    (doc_id,),
                )
                rows = cur.fetchall()
        return [_row_to_version(r) for r in rows]

    def migrate_version_to_s3(self, doc_id: str, version_id: str, s3_key: str) -> bool:
        """Backfill step (see app/s3_backfill.py): same contract as
        the SQLite store — pointer swap only, after upload + hash
        verification by the caller. Idempotent."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE versions SET s3_key = %s, html = '' "
                    "WHERE id = %s AND document_id = %s AND s3_key = ''",
                    (s3_key, version_id, doc_id),
                )
                return cur.rowcount > 0

    def get_version(self, doc_id: str, version_id: str) -> Version | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM versions WHERE document_id = %s AND id = %s",
                    (doc_id, version_id),
                )
                row = cur.fetchone()
        return _row_to_version(row) if row else None

    # -- shared document state (KV) ------------------------------------
    def get_state(self, doc_id: str) -> dict:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, value FROM document_kv WHERE document_id = %s ORDER BY key",
                    (doc_id,),
                )
                rows = cur.fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def get_state_value(self, doc_id: str, key: str):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM document_kv WHERE document_id = %s AND key = %s",
                    (doc_id, key),
                )
                row = cur.fetchone()
        return json.loads(row["value"]) if row else None

    def set_state_value(self, doc_id: str, key: str, value, actor: str) -> bool:
        validate_state_key(key)
        if value is None:
            self.delete_state_value(doc_id, key)
            return False
        validate_state_value(value)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO document_kv (document_id, key, value, actor, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (document_id, key) DO UPDATE SET
                        value = EXCLUDED.value, actor = EXCLUDED.actor,
                        updated_at = now()
                    """,
                    (doc_id, key, json.dumps(value), actor),
                )
        return True

    def delete_state_value(self, doc_id: str, key: str) -> bool:
        validate_state_key(key)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_kv WHERE document_id = %s AND key = %s",
                    (doc_id, key),
                )
                return cur.rowcount > 0

    def patch_state(self, doc_id: str, patch: dict, actor: str) -> dict:
        if len(patch) > _MAX_PATCH_KEYS:
            raise ValueError("state_patch_too_large")
        for key, value in patch.items():
            self.set_state_value(doc_id, key, value, actor)
        return self.get_state(doc_id)

    # -- API registry --------------------------------------------------
    def register_api(self, name: str, api_url: str, token_url: str, scope: str, user_id: str, app: str | None = None, swagger_url: str | None = None) -> dict | None:
        validate_api_registration(name, api_url, token_url)
        if app is not None and not _API_NAME_RE.match(app):
            raise ValueError("invalid_api_name")
        user_id = (user_id or "").strip() or "anonymous"
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO apis (name, api_url, token_url, scope, actor, user_id, app, swagger_url, endpoints) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '[]')",
                        (name, api_url, token_url, scope or "", user_id, user_id, app or name, swagger_url or ""),
                    )
        except self._psycopg.IntegrityError:
            return None
        return self.get_api(name)

    def list_apis(self, app: str | None = None, user_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if app:
            clauses.append("app = %s")
            params.append(app)
        if user_id:
            clauses.append("user_id = %s")
            params.append(user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM apis {where} ORDER BY name", params)
                rows = cur.fetchall()
        return [_row_dict(r) for r in rows]

    def get_api(self, name: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM apis WHERE name = %s", (name,))
                row = cur.fetchone()
        return _row_dict(row) if row else None

    def update_api(self, name: str, api_url: str | None, token_url: str | None, scope: str | None, app: str | None = None, swagger_url: str | None = None, endpoints: list[str] | None = None) -> dict | None:
        existing = self.get_api(name)
        if existing is None:
            return None
        new_api_url = api_url if api_url is not None else existing["api_url"]
        new_token_url = token_url if token_url is not None else existing["token_url"]
        validate_api_registration(name, new_api_url, new_token_url)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE apis SET api_url = %s, token_url = %s, scope = %s, app = %s,
                        swagger_url = %s, endpoints = %s, updated_at = now()
                    WHERE name = %s
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
            with conn.cursor() as cur:
                cur.execute("DELETE FROM apis WHERE name = %s", (name,))
                return cur.rowcount > 0
