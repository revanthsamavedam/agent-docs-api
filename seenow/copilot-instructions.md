# Canvas API — Copilot instructions (repo: canvas-api)

You are building the Canvas API service from scratch. Canvas is a
governed publication and audit layer for agent-built documents:
agents publish plain, self-contained HTML, people view it, every
version is kept. This repo is the API only. Do not build the viewer,
CLI, or MCP server here — those are separate repos with their own
instructions. This repo must not depend on them.

## Authentication: unauthenticated for now

- **Every endpoint in this repo is unauthenticated for now.** Do
  not build login, token verification, OAuth, session cookies, or
  RBAC. The maintainer will wire authentication later and will say
  so explicitly when that changes.
- Until then, use one fixed actor for every audit, registry, and
  usage record: `anonymous`. Do not invent users, do not treat a
  header as a verified identity, and do not store credentials.
- The registry stores URLs only — never tokens, secrets, or
  credentials. A `token_url` column exists for later use; in
  unauthenticated mode it is stored but never used, and the
  viewer bridge must never receive it.
- Never put a secret, token, or credential in code, logs, test
  fixtures, or error responses. Publish-validation errors must
  never echo a secret.

## Stack

- Python + FastAPI, uvicorn. Entrypoint `app.main:main`, port
  from `PORT`, default 8000.
- Postgres in production, SQLite in dev/tests, with the SAME
  method interface and identical semantics in `app/store.py`
  (SQLite) and `app/postgres_store.py` (Postgres). `app/main.py`
  must never fork on backend type. SQLite uses TEXT equivalents
  (JSON as TEXT, CITEXT as TEXT with COLLATE NOCASE).
- S3-compatible object storage, private bucket, via
  `app/blob_store.py`. The API talks to S3; the viewer never does.
- Tests: pytest from the repo root.

## Postgres schema (the authority)

Fresh database only, not a migration. The Postgres store runs this
file on init (`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE
VIEW` only); a deployer can also run it by hand with `psql -f`.
One database per environment — no env column anywhere.

```sql
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(), ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(), upd_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_no INT NOT NULL, content_sha256 TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(), ins_by TEXT NOT NULL,
    PRIMARY KEY (document_id, version_no)
);

CREATE TABLE IF NOT EXISTS document_state (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    key TEXT NOT NULL, value JSONB NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(), ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(), upd_by TEXT NOT NULL,
    PRIMARY KEY (document_id, key)
);

CREATE TABLE IF NOT EXISTS api_registry (
    app_name CITEXT NOT NULL PRIMARY KEY, base_url TEXT NOT NULL,
    token_url TEXT, swagger_url TEXT,
    endpoints JSONB NOT NULL DEFAULT '[]', endpoints_synced_at TIMESTAMPTZ,
    user_id CITEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now(), ins_by TEXT NOT NULL,
    upd_ts TIMESTAMPTZ NOT NULL DEFAULT now(), upd_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq BIGSERIAL PRIMARY KEY, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}', prev_hash TEXT,
    entry_hash TEXT NOT NULL, ins_ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id BIGSERIAL PRIMARY KEY, document_id TEXT NOT NULL,
    version_no INT, app_name CITEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('GET','POST','PUT','DELETE')),
    path TEXT NOT NULL, status_code INT, error TEXT,
    duration_ms INT NOT NULL, actor TEXT NOT NULL,
    called_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_call_log_doc_idx ON api_call_log (document_id, called_at DESC);
CREATE INDEX IF NOT EXISTS api_call_log_app_idx ON api_call_log (app_name, called_at DESC);
CREATE INDEX IF NOT EXISTS api_call_log_actor_idx ON api_call_log (actor, called_at DESC);

CREATE TABLE IF NOT EXISTS api_sync_log (
    id BIGSERIAL PRIMARY KEY,
    app_name CITEXT NOT NULL REFERENCES api_registry(app_name) ON DELETE CASCADE,
    spec_sha256 TEXT, added INT NOT NULL DEFAULT 0,
    removed JSONB NOT NULL DEFAULT '[]', synced_by TEXT NOT NULL,
    ins_ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW document_latest AS
SELECT d.id, d.title, d.author, v.version_no AS latest_version_no,
    v.s3_key, v.content_sha256, v.ins_ts AS published_ts, v.ins_by AS published_by
FROM documents d
LEFT JOIN LATERAL (
    SELECT version_no, s3_key, content_sha256, ins_ts, ins_by
    FROM document_versions WHERE document_id = d.id
    ORDER BY version_no DESC LIMIT 1
) v ON true;
```

- `documents`: no current-version pointer, no S3 path. Latest is
  derived as `max(version_no)`.
- `document_state`: shared KV, no version column. Changing it
  never creates a version; it survives versions; null = delete row.
- `api_registry`: PK is `app_name` CITEXT alone, one shared row
  per app per deployment. `user_id` is audit only (`anonymous`
  for now), never access control.
- `audit_events`: hash-chained, append-only:
  `entry_hash = sha256(prev_hash || entity_type || entity_id ||
  action || actor || canonical details JSON)`. Replay in `seq`
  order to verify. Never UPDATE or DELETE.
- No tables for users, tokens, grants, or shares.

## Endpoints (build exactly these, unauthenticated)

- `GET /health`
- `POST /v1/documents` (201), body `{title, html}` — creates the
  document AND version 1 in one call. No draft-only path.
- `GET /v1/documents`, `GET /v1/documents/{doc_id}`
- `POST /v1/documents/{doc_id}/versions` (201), body `{html}` only
- `GET /v1/documents/{doc_id}/versions`,
  `GET /v1/documents/{doc_id}/versions/{number}`,
  `GET /v1/documents/{doc_id}/versions/{number}/html`
- `POST /v1/apis` (201), `GET /v1/apis`,
  `GET /v1/apis/{app_name}`, `PUT /v1/apis/{app_name}`,
  `DELETE /v1/apis/{app_name}`
- `POST /v1/apis/{app_name}/sync`
- `POST /v1/usage` (201)
- `GET /v1/documents/{doc_id}/state`,
  `POST /v1/documents/{doc_id}/state` (patch),
  `GET /v1/documents/{doc_id}/state/{key}`,
  `PUT /v1/documents/{doc_id}/state/{key}`,
  `DELETE /v1/documents/{doc_id}/state/{key}`

## How to add an endpoint (in order)

1. Store method first, in BOTH stores, identical semantics.
2. Endpoint in `app/main.py`, Pydantic model, required fields
   `Field(min_length=1)`, optional URL fields `Optional[...] = None`.
3. Validate at the boundary (see below).
4. Write one `audit_events` row for every create, publish,
   registry register/update/delete, and sync. A failed audit write
   is 500, never silently skipped.
5. Tests for the new endpoint; run pytest until green. Postgres
   tests need a live server — if none is available, say so
   explicitly, never fake Postgres results. SQLite tests must pass.
6. Update README if the endpoint is user-facing.

## Publish, version, state, registry, usage rules

- Document ids are `doc_` + 12 readable chars (not UUIDs);
  regenerate on PK collision.
- Publish appends: `next = coalesce(max(version_no), 0) + 1` in
  the publish transaction. The `(document_id, version_no)` PK is
  the concurrency control; on collision, recompute and retry once.
  No base-version, no 409 protocol.
- Upload to S3 FIRST, then insert the version row. Key:
  `docs/<document_id>/v<version_no>.html`. An orphaned object
  after a failed insert is acceptable; retry to the next key.
- Version content lives ONLY in S3. No inline HTML, no Postgres
  `content` column, no `storage` flag, no backfill. The bucket
  stays private; the API verifies bytes against `content_sha256`
  before serving (missing object, hash mismatch, or unconfigured
  store = 500, not 404).
- Versions by NUMBER. Unknown number = 404, never fall back to
  latest. No version requested = latest = `max(version_no)`.
- Publish-time HTML validation gates create + publish with
  400 `html_validation_failed`; a rejected publish stores nothing.
  Reject: `secret_detected`, `token_url_in_html`,
  `external_resource` (documents must be self-contained: no
  external scripts, stylesheets, images, iframes, media, form
  actions, CSS imports, or JS module imports), `react_import`.
  Live data belongs at view time via the viewer bridge, not baked
  into HTML.
- No document size cap by design; a deployer enforces limits at
  the reverse proxy, not here.
- State: document-scoped, not version-scoped; key charset/length
  validated, 64 KiB value cap, 256 keys per patch; null = delete.
- Registry: fields are `app_name` / `base_url`, nullable
  `token_url` / `swagger_url`, no `scope` field, no `app` list
  filter. Names are slugs `^[a-z0-9][a-z0-9_\-]{0,63}$`; URLs
  https (http only for localhost dev). Sync: base URL from the
  spec's `servers[0].url`; token URL from the spec's Swagger
  oauth2 `tokenUrl` if present, else keep the existing one (spec
  paths alone never invent one); endpoints from spec paths;
  removed endpoints are REPORTED, never auto-deleted; one
  `api_sync_log` row per sync; sync is owner-triggered, never by
  the viewer at view time.
- Usage (`POST /v1/usage`): body `document_id`, `version_no`,
  `app_name`, `method` (GET/POST/PUT/DELETE only, else 400),
  `path`, `status_code` (NULL when no response), `error`,
  `duration_ms`. Actor is `anonymous` for now, never
  client-supplied. Strip query strings server-side before storing.
  NEVER store query strings, bodies, headers, or tokens. Prefer
  the endpoint template from the registry snapshot over a raw path.
  Beacon failures must never break a document.

## Never reintroduce

Drafts, `?draft=1`, `draft_html`, approval gates, version ids,
`base_version_id`, 409 flow, `change_summary`, `byte_size`,
inline HTML in version rows, S3 backfill, comments, reactions,
mentions, notifications, grants, sharing UI, authentication,
login, token verification, RBAC, per-env columns, per-user
registry scoping, users/tokens tables.
