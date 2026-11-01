-- agent-docs Postgres schema. Idempotent: safe to run on a fresh
-- database or an existing one created before drafts / app grouping /
-- user_id existed. The PostgresStore runs this file on init; it is
-- also the script a deployer runs by hand (psql -f postgres_schema.sql).
--
-- Credentials never live here. The service connects with the
-- already-governed DB service account, via DATABASE_URL in the
-- service tier — agents and users never receive DB credentials.

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    actor TEXT NOT NULL,
    draft_html TEXT NOT NULL DEFAULT '',
    draft_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, number)
);

CREATE TABLE IF NOT EXISTS document_kv (
    document_id TEXT NOT NULL REFERENCES documents(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    actor TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, key)
);

CREATE TABLE IF NOT EXISTS apis (
    name TEXT PRIMARY KEY,
    api_url TEXT NOT NULL,
    token_url TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    app TEXT NOT NULL DEFAULT '',
    swagger_url TEXT NOT NULL DEFAULT '',
    endpoints TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migrations for databases created before these columns existed.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS draft_html TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS draft_updated_at TIMESTAMPTZ;
ALTER TABLE apis ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'anonymous';
ALTER TABLE apis ADD COLUMN IF NOT EXISTS app TEXT NOT NULL DEFAULT '';
ALTER TABLE apis ADD COLUMN IF NOT EXISTS swagger_url TEXT NOT NULL DEFAULT '';
ALTER TABLE apis ADD COLUMN IF NOT EXISTS endpoints TEXT NOT NULL DEFAULT '[]';

-- Backfills, same as the SQLite migration: app defaults to the
-- registry name, and a pre-user_id row takes user_id from actor.
UPDATE apis SET app = name WHERE app = '';
UPDATE apis SET user_id = actor WHERE user_id = 'anonymous' AND actor <> '' AND actor <> 'anonymous';
