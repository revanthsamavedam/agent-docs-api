-- agent-docs Postgres schema (new-schema, agreed 2026-11-02).
-- Idempotent: safe to run on a fresh database. It is NOT a
-- migration from the old schema (documents/versions/apis with
-- draft columns, version ids, and inline HTML): this is a
-- breaking redesign, so it expects a fresh database. The
-- PostgresStore runs this file on init; it is also the script
-- a deployer runs by hand (psql -f postgres_schema.sql).
--
-- Design notes (decisions, not suggestions):
-- - No drafts, no approvals, no version ids, no base_version_id,
--   no change_summary, no byte_size column.
-- - Version content lives ONLY in S3: document_versions holds
--   s3_key + content_sha256, never HTML. The API serves bytes
--   from S3 after verifying the hash; the viewer never talks
--   to S3 and never sees a bucket URL.
-- - Publish appends the next version: next =
--   coalesce(max(version_no), 0) + 1. The (document_id,
--   version_no) primary key is the concurrency control: on a
--   collision the publisher recomputes and retries once.
-- - The registry is per deployment (one database per
--   environment); there is no env column. app_name is the whole
--   identity, case-insensitive via CITEXT. user_id is audit
--   only, never access control.
-- - api_call_log is the live-call usage log: the viewer bridge
--   reports each apiFetch here (fire-and-forget). It never
--   holds query strings, bodies, headers, or tokens.
-- - audit_events is hash-chained and append-only: entry_hash =
--   sha256(prev_hash || entity_type || entity_id || action ||
--   actor || canonical details JSON). Verify by replaying rows
--   in seq order.
--
-- Credentials never live here. The service connects with the
-- already-governed DB service account, via DATABASE_URL in the
-- service tier — agents and users never receive DB credentials.

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    upd_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_no INT NOT NULL,
    content_sha256 TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ins_by TEXT NOT NULL,
    PRIMARY KEY (document_id, version_no)
);

CREATE TABLE IF NOT EXISTS document_state (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    upd_by TEXT NOT NULL,
    PRIMARY KEY (document_id, key)
);

CREATE TABLE IF NOT EXISTS api_registry (
    app_name CITEXT NOT NULL PRIMARY KEY,
    base_url TEXT NOT NULL,
    token_url TEXT,
    swagger_url TEXT,
    endpoints JSONB NOT NULL DEFAULT '[]',
    endpoints_synced_at TIMESTAMPTZ,
    user_id CITEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    upd_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    prev_hash TEXT,
    entry_hash TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL,
    version_no INT,
    app_name CITEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('GET', 'POST', 'PUT', 'DELETE')),
    path TEXT NOT NULL,
    status_code INT,
    error TEXT,
    duration_ms INT NOT NULL,
    actor TEXT NOT NULL,
    called_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS api_call_log_doc_idx
    ON api_call_log (document_id, called_at DESC);

CREATE INDEX IF NOT EXISTS api_call_log_app_idx
    ON api_call_log (app_name, called_at DESC);

CREATE INDEX IF NOT EXISTS api_call_log_actor_idx
    ON api_call_log (actor, called_at DESC);

CREATE TABLE IF NOT EXISTS api_sync_log (
    id BIGSERIAL PRIMARY KEY,
    app_name CITEXT NOT NULL REFERENCES api_registry(app_name) ON DELETE CASCADE,
    spec_sha256 TEXT,
    added INT NOT NULL DEFAULT 0,
    removed JSONB NOT NULL DEFAULT '[]',
    synced_by TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Latest-version view. Docs with no versions still appear, with
-- NULL version columns — that is how an unpublished doc reads.
CREATE OR REPLACE VIEW document_latest AS
SELECT
    d.id,
    d.title,
    d.author,
    v.version_no AS latest_version_no,
    v.s3_key,
    v.content_sha256,
    v.ins_ts AS published_ts,
    v.ins_by AS published_by
FROM documents d
LEFT JOIN LATERAL (
    SELECT version_no, s3_key, content_sha256, ins_ts, ins_by
    FROM document_versions
    WHERE document_id = d.id
    ORDER BY version_no DESC
    LIMIT 1
) v ON true;
