"""Postgres store for agent-docs: same repository interface as the
SQLite Store, selected when DATABASE_URL starts with
postgresql:// or postgres:// (see store.get_store).

The schema lives in postgres_schema.sql next to this file and is
run on init; it is idempotent on a fresh database. This is a
breaking redesign (no drafts, no version ids, S3-only version
content, api_registry keyed by app_name) — it does not migrate
databases created under the old schema.

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
    _UNSET,
    _audit_hash,
    _new_id,
    _USAGE_METHODS,
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
    d = {k: _text(v) for k, v in dict(row).items()}
    if isinstance(d.get("endpoints"), str):
        d["endpoints"] = json.loads(d["endpoints"] or "[]")
    if isinstance(d.get("details"), str):
        d["details"] = json.loads(d["details"] or "{}")
    if isinstance(d.get("removed"), str):
        d["removed"] = json.loads(d["removed"] or "[]")
    return d


def _row_to_version(row) -> Version:
    return Version(
        row["document_id"],
        row["version_no"],
        row["content_sha256"],
        row["s3_key"],
        row["ins_by"],
        _text(row["ins_ts"]),
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
        # Blob store is REQUIRED for publishing: version HTML goes
        # to the blob store and the row holds only s3_key. None
        # means publishing fails, by design.
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
        # comment sits in the same chunk as the first statement).
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
        self, title: str, html: str, actor: str
    ) -> tuple[Document, Version]:
        if self.blob_store is None:
            raise RuntimeError("blob_store_not_configured")
        doc_id = None
        for _ in range(3):
            candidate = _new_id("doc")
            try:
                with self._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO documents (id, title, author, ins_by, upd_by) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (candidate, title, actor, actor, actor),
                        )
                doc_id = candidate
                break
            except self._psycopg.IntegrityError:
                continue
        if doc_id is None:
            raise RuntimeError("document_id_collision")
        version = self._insert_version(doc_id, 1, html, actor)
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
        return Document(row["id"], row["title"], row["author"], _text(row["ins_ts"]))

    def list_documents(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id, d.title, d.ins_ts::text AS created_at,
                           (SELECT MAX(version_no) FROM document_versions v
                             WHERE v.document_id = d.id) AS latest_version
                    FROM documents d ORDER BY d.ins_ts DESC, d.id DESC
                    """
                )
                rows = cur.fetchall()
        return [_row_dict(r) for r in rows]

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
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO document_versions
                      (document_id, version_no, content_sha256, s3_key, ins_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (doc_id, number, sha, s3_key, actor),
                )
                cur.execute(
                    "SELECT * FROM document_versions "
                    "WHERE document_id = %s AND version_no = %s",
                    (doc_id, number),
                )
                row = cur.fetchone()
        assert row is not None
        return _row_to_version(row)

    def _next_version_number(self, doc_id: str) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(version_no) AS m FROM document_versions "
                    "WHERE document_id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()
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
            except self._psycopg.IntegrityError as e:
                last_exc = e
        raise last_exc  # pragma: no cover - lost two races in a row

    def latest_version(self, doc_id: str) -> Version | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_versions WHERE document_id = %s "
                    "ORDER BY version_no DESC LIMIT 1",
                    (doc_id,),
                )
                row = cur.fetchone()
        return _row_to_version(row) if row else None

    def list_versions(self, doc_id: str) -> list[Version]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_versions WHERE document_id = %s "
                    "ORDER BY version_no ASC",
                    (doc_id,),
                )
                rows = cur.fetchall()
        return [_row_to_version(r) for r in rows]

    def get_version(self, doc_id: str, number: int) -> Version | None:
        """Address a version by its NUMBER, never by an id (there
        are no version ids). Unknown number -> None (the endpoint
        turns this into 404, never a fallback to latest)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_versions "
                    "WHERE document_id = %s AND version_no = %s",
                    (doc_id, number),
                )
                row = cur.fetchone()
        return _row_to_version(row) if row else None

    # -- shared document state (KV) ------------------------------------
    def get_state(self, doc_id: str) -> dict:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, value FROM document_state WHERE document_id = %s ORDER BY key",
                    (doc_id,),
                )
                rows = cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_state_value(self, doc_id: str, key: str):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM document_state WHERE document_id = %s AND key = %s",
                    (doc_id, key),
                )
                row = cur.fetchone()
        return row["value"] if row else None

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
                    INSERT INTO document_state (document_id, key, value, ins_by, upd_by, upd_ts)
                    VALUES (%s, %s, %s::jsonb, %s, %s, now())
                    ON CONFLICT (document_id, key) DO UPDATE SET
                        value = EXCLUDED.value, upd_by = EXCLUDED.upd_by,
                        upd_ts = now()
                    """,
                    (doc_id, key, json.dumps(value), actor, actor),
                )
        return True

    def delete_state_value(self, doc_id: str, key: str) -> bool:
        validate_state_key(key)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_state WHERE document_id = %s AND key = %s",
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
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO api_registry (app_name, base_url, token_url, swagger_url, endpoints, user_id, ins_by, upd_by) "
                        "VALUES (%s, %s, %s, %s, '[]', %s, %s, %s)",
                        (app_name, base_url, token_url, swagger_url, user_id, user_id, user_id),
                    )
        except self._psycopg.IntegrityError:
            return None
        return self.get_api(app_name)

    def list_apis(self, user_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM api_registry WHERE user_id = %s ORDER BY app_name",
                        (user_id,),
                    )
                else:
                    cur.execute("SELECT * FROM api_registry ORDER BY app_name")
                rows = cur.fetchall()
        return [_row_dict(r) for r in rows]

    def get_api(self, app_name: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM api_registry WHERE app_name = %s", (app_name,))
                row = cur.fetchone()
        return _row_dict(row) if row else None

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
            with conn.cursor() as cur:
                if endpoints is not _UNSET:
                    cur.execute(
                        """
                        UPDATE api_registry SET base_url = %s, token_url = %s,
                            swagger_url = %s, endpoints = %s::jsonb,
                            endpoints_synced_at = now(), upd_ts = now()
                        WHERE app_name = %s
                        """,
                        (new_base, new_token, new_swagger, json.dumps(new_endpoints), app_name),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE api_registry SET base_url = %s, token_url = %s,
                            swagger_url = %s, upd_ts = now()
                        WHERE app_name = %s
                        """,
                        (new_base, new_token, new_swagger, app_name),
                    )
        return self.get_api(app_name)

    def delete_api(self, app_name: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_registry WHERE app_name = %s", (app_name,))
                return cur.rowcount > 0

    def log_sync(
        self, app_name: str, spec_sha256: str | None, added: int,
        removed: list, synced_by: str,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_sync_log (app_name, spec_sha256, added, removed, synced_by) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s)",
                    (app_name, spec_sha256, added, json.dumps(removed), synced_by),
                )

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
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_call_log
                      (document_id, version_no, app_name, method, path,
                       status_code, error, duration_ms, actor)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
                )
                prev = cur.fetchone()
                prev_hash = prev["entry_hash"] if prev else None
                entry_hash = _audit_hash(
                    prev_hash, entity_type, entity_id, action, actor, details
                )
                cur.execute(
                    """
                    INSERT INTO audit_events
                      (entity_type, entity_id, action, actor, details,
                       prev_hash, entry_hash)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING *
                    """,
                    (entity_type, entity_id, action, actor,
                     json.dumps(details, sort_keys=True), prev_hash, entry_hash),
                )
                return _row_dict(cur.fetchone())

    def list_audit(self, entity_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if entity_id:
                    cur.execute(
                        "SELECT * FROM audit_events WHERE entity_id = %s ORDER BY seq ASC",
                        (entity_id,),
                    )
                else:
                    cur.execute("SELECT * FROM audit_events ORDER BY seq ASC")
                rows = cur.fetchall()
        return [_row_dict(r) for r in rows]
