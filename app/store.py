"""SQLite store behind a small repository layer (new-schema).

Same method interface as PostgresStore (app/postgres_store.py),
so main.py never forks. Selected when DATABASE_URL is unset or
not postgresql:// (see get_store).

New-schema decisions (agreed 2026-11-02), mirrored here with
TEXT equivalents of the Postgres types:
  - No drafts, no approvals, no version ids, no base_version_id,
    no change_summary, no byte_size column.
  - Version content lives ONLY in the blob store (S3 in prod,
    InMemoryBlobStore in tests): document_versions holds s3_key
    + content_sha256, never HTML. Publishing without a blob
    store fails, by design.
  - Publish appends the next version: next =
    coalesce(max(version_no), 0) + 1. The (document_id,
    version_no) primary key is the concurrency control: on a
    collision the publisher recomputes and retries once.
  - Registry: api_registry keyed by app_name (COLLATE NOCASE,
    the SQLite stand-in for CITEXT). token_url/swagger_url
    nullable; no scope column; user_id is audit only.
  - JSONB becomes TEXT holding JSON; TIMESTAMPTZ becomes TEXT
    (datetime('now')); CITEXT becomes TEXT COLLATE NOCASE.

DATABASE_URL handling:
  - unset            -> ./agentdocs.db next to the working directory
  - sqlite:///path   -> that file
  - sqlite:///:memory: -> in-memory (tests)
  - a plain path     -> that file
  - postgresql://... -> PostgresStore (app/postgres_store.py,
    schema in app/postgres_schema.sql; needs psycopg, see the
    [postgres] extra in pyproject.toml)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
from dataclasses import dataclass

# API registry: app short name -> (base_url, token_url,
# swagger_url, endpoints). The app short name (e.g. "refdp") is
# the registry key; endpoints are paths from its Swagger doc.
# Names are slugs; URLs must be https (http only for localhost
# dev). The registry stores URLs only — never tokens or secrets.
# A token URL is fetched at view time, per viewer, per endpoint.
# token_url is nullable: a missing token URL means no token
# fetch (apiKey/bearer schemes). swagger_url is stored so a
# later sync can fetch the latest Swagger without the caller
# re-supplying it.
_API_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def validate_api_registration(
    app_name: str, base_url: str, token_url: str | None = None
) -> None:
    if not _API_NAME_RE.match(app_name):
        raise ValueError("invalid_api_name")
    for url in (base_url, token_url):
        if url is None or url == "":
            continue
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

# Live-call usage log: only these methods may be logged (the
# table CHECK enforces the same list).
_USAGE_METHODS = ("GET", "POST", "PUT", "DELETE")

# Sentinel for update_api: distinguishes "leave this column
# alone" from "set it (possibly to NULL)".
_UNSET = object()


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


def _audit_hash(prev_hash, entity_type, entity_id, action, actor, details) -> str:
    """Hash-chain step: sha256(prev_hash || fields || details).

    Canonical details JSON (sorted keys) so the chain replays
    deterministically in seq order. The first entry chains from
    the empty string.
    """
    canonical = json.dumps(details, sort_keys=True, separators=(",", ":"))
    payload = "|".join(
        [prev_hash or "", entity_type, entity_id, action, actor, canonical]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sqlite_path_from_database_url(database_url: str | None) -> str:
    if not database_url:
        return "agentdocs.db"
    if database_url.startswith("postgresql://") or database_url.startswith(
        "postgres://"
    ):
        raise RuntimeError(
            "Store is the SQLite backend; use get_store() for a "
            "postgresql:// DATABASE_URL (it returns PostgresStore)."
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
    author: str
    created_at: str


@dataclass
class Version:
    document_id: str
    number: int
    sha256: str
    s3_key: str
    actor: str
    created_at: str


def _new_id(prefix: str) -> str:
    """Short readable id: prefix + 12 chars from a URL-safe alphabet.

    Not a UUID on purpose: ids appear in URLs and logs, where a
    24-hex blob is unreadable. Collision odds at 12 chars are
    negligible, and the primary key catches one anyway (the
    caller retries).
    """
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits
    return f"{prefix}_" + "".join(secrets.choice(alphabet) for _ in range(12))


class Store:
    """SQLite-backed repository. All SQL lives here, nowhere else."""

    def __init__(self, database_url: str | None = None, blob_store=None) -> None:
        # blob_store (app.blob_store, e.g. S3BlobStore): REQUIRED
        # for publishing — version HTML goes to the blob store
        # and the row holds only s3_key. None means publishing
        # fails; reads of existing versions fail the same way.
        self.blob_store = blob_store
        self.path = _sqlite_path_from_database_url(database_url)
        # check_same_thread=False: FastAPI may serve from multiple threads;
        # each method opens its own short-lived connection.
        self._connect().close()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # ON DELETE CASCADE on documents -> versions/state only
        # works with foreign keys enabled (off by default).
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    ins_by TEXT NOT NULL,
                    upd_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    upd_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_versions (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    s3_key TEXT NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    ins_by TEXT NOT NULL,
                    PRIMARY KEY (document_id, version_no)
                );
                CREATE TABLE IF NOT EXISTS document_state (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    ins_by TEXT NOT NULL,
                    upd_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    upd_by TEXT NOT NULL,
                    PRIMARY KEY (document_id, key)
                );
                CREATE TABLE IF NOT EXISTS api_registry (
                    app_name TEXT COLLATE NOCASE PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    token_url TEXT,
                    swagger_url TEXT,
                    endpoints TEXT NOT NULL DEFAULT '[]',
                    endpoints_synced_at TEXT,
                    user_id TEXT COLLATE NOCASE NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    ins_by TEXT NOT NULL,
                    upd_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    upd_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    prev_hash TEXT,
                    entry_hash TEXT NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS api_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    version_no INTEGER,
                    app_name TEXT COLLATE NOCASE NOT NULL,
                    method TEXT NOT NULL CHECK (method IN ('GET', 'POST', 'PUT', 'DELETE')),
                    path TEXT NOT NULL,
                    status_code INTEGER,
                    error TEXT,
                    duration_ms INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    called_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS api_call_log_doc_idx
                    ON api_call_log (document_id, called_at DESC);
                CREATE INDEX IF NOT EXISTS api_call_log_app_idx
                    ON api_call_log (app_name, called_at DESC);
                CREATE INDEX IF NOT EXISTS api_call_log_actor_idx
                    ON api_call_log (actor, called_at DESC);
                CREATE TABLE IF NOT EXISTS api_sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT COLLATE NOCASE NOT NULL REFERENCES api_registry(app_name) ON DELETE CASCADE,
                    spec_sha256 TEXT,
                    added INTEGER NOT NULL DEFAULT 0,
                    removed TEXT NOT NULL DEFAULT '[]',
                    synced_by TEXT NOT NULL,
                    ins_ts TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE VIEW IF NOT EXISTS document_latest AS
                SELECT d.id, d.title, d.author,
                       v.version_no AS latest_version_no, v.s3_key,
                       v.content_sha256, v.ins_ts AS published_ts,
                       v.ins_by AS published_by
                FROM documents d
                LEFT JOIN document_versions v ON v.document_id = d.id
                    AND v.version_no = (
                        SELECT MAX(version_no) FROM document_versions
                        WHERE document_id = d.id
                    );
                """
            )

    # -- documents -----------------------------------------------------
    def create_document(
        self, title: str, html: str, actor: str
    ) -> tuple[Document, Version]:
        if self.blob_store is None:
            raise RuntimeError("blob_store_not_configured")
        doc_id = None
        for _ in range(3):
            candidate = _new_id("doc")
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO documents (id, title, author, ins_by, upd_by) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (candidate, title, actor, actor, actor),
                    )
                doc_id = candidate
                break
            except sqlite3.IntegrityError:
                continue
        if doc_id is None:
            raise RuntimeError("document_id_collision")
        version = self._insert_version(doc_id, 1, html, actor)
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
        return Document(row["id"], row["title"], row["author"], row["ins_ts"])

    def list_documents(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.title, d.ins_ts AS created_at,
                       (SELECT MAX(version_no) FROM document_versions v
                         WHERE v.document_id = d.id) AS latest_version
                FROM documents d ORDER BY d.ins_ts DESC, d.id DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # -- versions ------------------------------------------------------
    def _insert_version(
        self, doc_id: str, number: int, html: str, actor: str
    ) -> Version:
        if self.blob_store is None:
            raise RuntimeError("blob_store_not_configured")
        from .blob_store import version_blob_key

        # Upload first, pointer second: if the upload fails, no row
        # is created, so no row can ever point at a missing object.
        s3_key = version_blob_key(doc_id, number)
        self.blob_store.put(s3_key, html.encode("utf-8"))
        sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO document_versions
                  (document_id, version_no, content_sha256, s3_key, ins_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (doc_id, number, sha, s3_key, actor),
            )
            row = conn.execute(
                "SELECT * FROM document_versions "
                "WHERE document_id = ? AND version_no = ?",
                (doc_id, number),
            ).fetchone()
        assert row is not None
        return self._row_to_version(row)

    def _next_version_number(self, doc_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version_no) AS m FROM document_versions "
                "WHERE document_id = ?",
                (doc_id,),
            ).fetchone()
        return (row["m"] or 0) + 1

    def publish_version(self, doc_id: str, html: str, actor: str) -> Version:
        """Append the next version. The primary key is the
        concurrency control: if two publishers race for the same
        number, the loser recomputes and retries once (its first
        upload is left as a harmless orphan object)."""
        if self.blob_store is None:
            raise RuntimeError("blob_store_not_configured")
        last_exc = None
        for _ in range(2):
            number = self._next_version_number(doc_id)
            try:
                return self._insert_version(doc_id, number, html, actor)
            except sqlite3.IntegrityError as e:
                last_exc = e
        raise last_exc  # pragma: no cover - lost two races in a row

    def latest_version(self, doc_id: str) -> Version | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM document_versions WHERE document_id = ?
                ORDER BY version_no DESC LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self, doc_id: str) -> list[Version]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM document_versions WHERE document_id = ?
                ORDER BY version_no ASC
                """,
                (doc_id,),
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, doc_id: str, number: int) -> Version | None:
        """Address a version by its NUMBER, never by an id (there
        are no version ids). Unknown number -> None (the endpoint
        turns this into 404, never a fallback to latest)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM document_versions
                WHERE document_id = ? AND version_no = ?
                """,
                (doc_id, number),
            ).fetchone()
        return self._row_to_version(row) if row else None

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> Version:
        return Version(
            row["document_id"],
            row["version_no"],
            row["content_sha256"],
            row["s3_key"],
            row["ins_by"],
            row["ins_ts"],
        )

    # -- shared document state (KV) ----------------------------------
    def get_state(self, doc_id: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM document_state WHERE document_id = ? ORDER BY key",
                (doc_id,),
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def get_state_value(self, doc_id: str, key: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM document_state WHERE document_id = ? AND key = ?",
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
                INSERT INTO document_state (document_id, key, value, ins_by, upd_by, upd_ts)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(document_id, key) DO UPDATE SET
                    value = excluded.value, upd_by = excluded.upd_by,
                    upd_ts = datetime('now')
                """,
                (doc_id, key, json.dumps(value), actor, actor),
            )
        return True

    def delete_state_value(self, doc_id: str, key: str) -> bool:
        validate_state_key(key)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM document_state WHERE document_id = ? AND key = ?",
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
    def register_api(
        self, app_name: str, base_url: str, token_url: str | None,
        user_id: str, swagger_url: str | None = None,
    ) -> dict | None:
        """Register an app, stamped with the registering user id.

        user_id is 'anonymous' when no token was supplied (see
        _user_id in main.py). This is a record, not access control:
        listing/filtering by user_id works, but nothing restricts
        reads or writes to the owning user — that needs verified
        identity first.
        """
        token_url = token_url or None
        swagger_url = swagger_url or None
        validate_api_registration(app_name, base_url, token_url)
        user_id = (user_id or "").strip() or "anonymous"
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO api_registry (app_name, base_url, token_url, swagger_url, endpoints, user_id, ins_by, upd_by) "
                    "VALUES (?, ?, ?, ?, '[]', ?, ?, ?)",
                    (app_name, base_url, token_url, swagger_url, user_id, user_id, user_id),
                )
        except sqlite3.IntegrityError:
            return None
        return self.get_api(app_name)

    def list_apis(self, user_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM api_registry WHERE user_id = ? ORDER BY app_name",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM api_registry ORDER BY app_name"
                ).fetchall()
        return [self._row_to_api(r) for r in rows]

    def get_api(self, app_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_registry WHERE app_name = ?", (app_name,)
            ).fetchone()
        return self._row_to_api(row) if row else None

    def update_api(
        self, app_name: str, base_url=_UNSET, token_url=_UNSET,
        swagger_url=_UNSET, endpoints=_UNSET,
    ) -> dict | None:
        """Update registry fields. A field left as _UNSET is kept;
        an explicit None clears a nullable column. Passing
        endpoints also stamps endpoints_synced_at (a sync ran)."""
        existing = self.get_api(app_name)
        if existing is None:
            return None
        new_base = base_url if base_url is not _UNSET else existing["base_url"]
        new_token = token_url if token_url is not _UNSET else existing["token_url"]
        new_swagger = swagger_url if swagger_url is not _UNSET else existing["swagger_url"]
        new_endpoints = endpoints if endpoints is not _UNSET else existing["endpoints"]
        validate_api_registration(app_name, new_base, new_token)
        with self._connect() as conn:
            if endpoints is not _UNSET:
                conn.execute(
                    """
                    UPDATE api_registry SET base_url = ?, token_url = ?,
                        swagger_url = ?, endpoints = ?,
                        endpoints_synced_at = datetime('now'),
                        upd_ts = datetime('now')
                    WHERE app_name = ?
                    """,
                    (new_base, new_token, new_swagger, json.dumps(new_endpoints), app_name),
                )
            else:
                conn.execute(
                    """
                    UPDATE api_registry SET base_url = ?, token_url = ?,
                        swagger_url = ?, upd_ts = datetime('now')
                    WHERE app_name = ?
                    """,
                    (new_base, new_token, new_swagger, app_name),
                )
        return self.get_api(app_name)

    def delete_api(self, app_name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM api_registry WHERE app_name = ?", (app_name,)
            )
            deleted = cur.rowcount > 0
        return deleted

    def log_sync(
        self, app_name: str, spec_sha256: str | None, added: int,
        removed: list, synced_by: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO api_sync_log (app_name, spec_sha256, added, removed, synced_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (app_name, spec_sha256, added, json.dumps(removed), synced_by),
            )

    @staticmethod
    def _row_to_api(row) -> dict:
        d = dict(row)
        d["endpoints"] = json.loads(d.get("endpoints") or "[]")
        return d

    # -- live-call usage log -------------------------------------------
    def log_usage(
        self, document_id: str, version_no: int | None, app_name: str,
        method: str, path: str, status_code: int | None, error: str | None,
        duration_ms: int, actor: str,
    ) -> None:
        """Append one beacon row. Best-effort by contract: the
        bridge ignores beacon failures, so this must never raise
        for a bad method — main.py validates first (400)."""
        if method not in _USAGE_METHODS:
            raise ValueError("invalid_usage_method")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_call_log
                  (document_id, version_no, app_name, method, path,
                   status_code, error, duration_ms, actor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, version_no, app_name, method, path,
                 status_code, error, duration_ms, actor),
            )

    # -- hash-chained audit log ----------------------------------------
    def log_audit(
        self, entity_type: str, entity_id: str, action: str,
        actor: str, details: dict | None = None,
    ) -> dict:
        """Append one audit entry, chained to the previous hash.

        entry_hash = sha256(prev_hash || entity_type || entity_id
        || action || actor || canonical details JSON). Replay rows
        in seq order to verify the chain. The first entry chains
        from the empty string.
        """
        details = details or {}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["entry_hash"] if row else None
            entry_hash = _audit_hash(
                prev_hash, entity_type, entity_id, action, actor, details
            )
            cur = conn.execute(
                """
                INSERT INTO audit_events
                  (entity_type, entity_id, action, actor, details,
                   prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entity_type, entity_id, action, actor,
                 json.dumps(details, sort_keys=True), prev_hash, entry_hash),
            )
            entry = conn.execute(
                "SELECT * FROM audit_events WHERE seq = ?", (cur.lastrowid,)
            ).fetchone()
        d = dict(entry)
        d["details"] = json.loads(d["details"])
        return d

    def list_audit(self, entity_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if entity_id:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE entity_id = ? ORDER BY seq ASC",
                    (entity_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_events ORDER BY seq ASC"
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out


def get_store():
    """Production store, selected from DATABASE_URL.

    postgresql:// or postgres:// -> PostgresStore (schema in
    app/postgres_schema.sql, psycopg required); anything else
    -> the SQLite Store. Endpoints never change: both stores
    implement the same methods.
    """
    url = os.environ.get("DATABASE_URL")
    if url and (url.startswith("postgresql://") or url.startswith("postgres://")):
        from .postgres_store import PostgresStore

        return PostgresStore(url)
    return Store(url)
