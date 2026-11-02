# agent-docs API — Copilot instructions

You are working in the agent-docs API service (FastAPI). It publishes,
views, and versions plain, self-contained HTML documents, plus shared
document state, an API registry, and a live-call usage log. Locked scope:
publish, view, version history, shared state, registry, usage log. There
are no comments, reactions, mentions, notifications, grants UI, or
approvals UI in this service. Do not add them.

## Never reintroduce removed concepts

This repo went through a breaking redesign (new-schema, 2026-11-02). The
following existed in git history and were deliberately removed. Do NOT
bring any of them back, even if old code or old tests suggest them:

- Drafts (draft documents, draft endpoints, `?draft=1`, `draft_html`).
- Approval gates / approvals tables / creator-approver checks.
- Version ids, `base_version_id`, and the 409 version-conflict flow.
- `change_summary` on versions, `byte_size` on versions.
- Inline HTML storage in version rows, S3 backfill scripts.

## Schema (Postgres, `app/postgres_schema.sql`)

The schema file is the authority; it expects a fresh database, not a
migration from the old schema. One database per environment (dev, staging,
prod each have their own); there is NO env column anywhere.

- `documents`: `id` TEXT PK, `title`, `author`, audit columns
  (`ins_ts`, `ins_by`, `upd_ts`, `upd_by`). No current-version pointer.
- `document_versions`: PK is `(document_id, version_no)`. Columns:
  `content_sha256`, `s3_key` (NOT NULL), audit columns. No HTML here.
- `document_state`: PK is `(document_id, key)`, `value` JSONB. State is
  document-scoped, NOT version-scoped: it survives versions, and changing
  it never creates a version. Null from the API means DELETE the row.
- `api_registry`: PK is `app_name` CITEXT (case-insensitive: `RefDP` and
  `refdp` are the same row). Columns: `base_url` (NOT NULL),
  `token_url` (NULLABLE), `swagger_url` (NULLABLE),
  `endpoints` JSONB, `endpoints_synced_at`, `user_id` CITEXT, audit
  columns. One shared row per app per deployment.
- `audit_events`: `seq` BIGSERIAL PK, `entity_type`, `entity_id`,
  `action`, `actor`, `details` JSONB, `prev_hash`, `entry_hash`.
  Hash-chained and append-only: `entry_hash = sha256(prev_hash ||
  entity_type || entity_id || action || actor || canonical details JSON)`.
  Verify by replaying rows in `seq` order. Never UPDATE or DELETE rows.
- `api_call_log`: the live-call usage log (see below).
- `api_sync_log`: one row per registry sync (added count, removed list).
- `document_latest` view: each document joined to its max-version row.
  Documents with no versions still appear, with NULL version columns.

## How to add an endpoint (checklist, in order)

1. **Store method first, in BOTH stores.** `app/store.py` (SQLite, dev
   and tests) and `app/postgres_store.py` (Postgres, production) implement
   the SAME method interface. Add the method to both, with identical
   semantics. `app/main.py` must never fork on backend type. SQLite uses
   TEXT equivalents (JSON as TEXT, CITEXT as TEXT with COLLATE NOCASE).
2. **Endpoint in `app/main.py`.** Add a Pydantic request model (required
   fields use `Field(min_length=1)`; optional URL/token fields are
   `Optional[...] = None`). Follow the existing envelope style:
   `{"version": ...}`, `{"api": ...}`, `{"documents": ...}`.
3. **Validate at the boundary.** Publish-time HTML validation
   (`app/validate_html.py`) gates create + publish with
   400 `html_validation_failed`; a rejected publish stores nothing and
   touches no version. Do not modify `validate_html.py` unless asked.
   State keys/values follow the rules in `store.py` (key charset and
   length, 64 KiB value cap, 256 keys per patch). API names are slugs
   (`^[a-z0-9][a-z0-9_\-]{0,63}$`); URLs must be https (http only for
   localhost dev).
4. **Write an audit row.** Every create, publish, registry register /
   update / delete, and sync writes one `audit_events` row. A failed
   audit write surfaces as 500 — never silently skip the record.
5. **Tests.** Add or update tests in `tests/` for every new endpoint and
   run `pytest` from the repo root until green. Postgres tests need a
   live server; if none is available, say so explicitly — never fake
   Postgres results. The SQLite-backed tests must pass.
6. **README.** Update `README.md` if the endpoint is user-facing.

## Publish and version rules

- Publish appends the next version: `next = coalesce(max(version_no), 0)
  + 1`, computed in the publish transaction. The
  `(document_id, version_no)` primary key IS the concurrency control: on
  a collision, recompute and retry once. There is no base-version or
  409 protocol.
- Upload to S3 FIRST, then insert the version row. Never the reverse: a
  row must never point at an object that does not exist yet. S3 key
  scheme: `docs/<document_id>/v<version_no>.html` (see
  `version_blob_key` in `app/blob_store.py`). If the row insert fails
  after upload, the orphaned object is acceptable; retry uploads to the
  next version's key.
- Version content lives ONLY in S3. There is no inline-HTML fallback.
  The bucket stays private: the API fetches bytes from S3 and verifies
  them against `content_sha256` before serving (missing object, hash
  mismatch, or unconfigured blob store = 500, not 404). The viewer never
  talks to S3 and never sees a bucket URL.
- Versions are addressed by NUMBER (`/v1/documents/{id}/versions/{n}`).
  An unknown number is 404. NEVER fall back to latest for an explicitly
  requested version. No version in the request means latest, which is
  `max(version_no)` (see the `document_latest` view).
- `POST /v1/documents` with `{title, html}` creates the document AND
  version 1 in one call. There is no draft flag and no draft-only
  creation path.
- Document ids are `doc_` + 12 readable chars (not UUIDs). On a PK
  collision, regenerate and retry.

## Registry rules

- Request/response fields are `app_name` / `base_url` (not `name` /
  `api_url`). There is no `scope` field and no `app` list filter.
- `token_url` NULL means no token fetch (apiKey/bearer schemes are
  valid). `swagger_url` NULL means a manually registered app with no
  spec; sync then requires a URL and fails 400 without one.
- Sync (`POST /v1/apis/{app_name}/sync`): base URL comes from the
  spec's `servers[0].url`; token URL comes from the spec's Swagger
  oauth2 `tokenUrl` if present, else the existing token URL is kept
  (spec paths alone never invent one). Endpoints come from spec paths.
  Removed endpoints are REPORTED, never auto-deleted. Write one
  `api_sync_log` row per sync. Sync is owner-triggered (never by the
  viewer at view time).
- `user_id` comes from the `Authorization` header passthrough (see
  Auth below) and is audit/lookup only. List filtering by `user_id`
  is a record lookup, NEVER access control.

## Usage log (`POST /v1/usage`)

- Body: `document_id`, `version_no` (the resolved version that made the
  call), `app_name`, `method` (GET/POST/PUT/DELETE only; anything else
  is 400), `path`, `status_code` (NULL when no response arrived),
  `error` (e.g. `network_error`, `timeout`, `unregistered_app`),
  `duration_ms`.
- The actor is derived SERVER-SIDE from the `Authorization` header.
  Never trust a client-supplied actor.
- Strip query strings from `path` server-side before storing, even if
  the caller already stripped them. NEVER store query strings, request
  or response bodies, headers, or tokens in this table. Prefer the
  endpoint template from the registry snapshot over a raw path when the
  path matches a known endpoint.

## Auth and secrets (do not build these)

- Auth is a placeholder seam ONLY: `_user_id` passes the `Authorization`
  header through as the user id, and `JWT_TOKEN_URL` is a literal
  placeholder constant the maintainer replaces in code before
  deployment. Do NOT verify tokens, name identity providers, or
  integrate any identity system here. The deployer wires real identity
  in front of this service.
- The registry stores URLs ONLY — never tokens, secrets, or credentials.
  Never put a secret, token, or credential in code, in a log message,
  in a test fixture, or in an error response. Publish-validation errors
  must never echo secrets.
- No document size cap by design: HTML is stored as-is. A deployer who
  wants a limit enforces it at the reverse proxy, not here.
