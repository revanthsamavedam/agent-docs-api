# agent-docs-api

Python (FastAPI) backend for agent-docs. Separate repo — it does not
import code from the cli, mcp, or ui repos, and they do not import
code from it. They talk to it over HTTP.

## Scope — publish, view, version history, and shared state

- Agents publish plain, self-contained HTML documents.
- Humans view documents and any past version.
- API registry: onboard an internal API once (base URL, token
  URL, Swagger sync); documents refer to it by app name.
- Live-call usage log: the viewer bridge reports each apiFetch
  for audit — never query strings, bodies, or tokens.
- Shared document state: a document-scoped key-value store.
  State survives across versions and changing it never creates
  a version. Keys match `[A-Za-z0-9._:-]{1,128}`, values are
  JSON up to 64 KiB, and a null value deletes a key.
  Document JavaScript reads/writes it via the viewer's bridge
  (`window.agentdocs.kv`).
- No comments, reactions, mentions, notifications, grants UI,
  or approvals UI.

Documents are never React/JSX. React is viewer chrome only, in the
ui repo.

## Database: SQLite for local dev, Postgres for production

All database access lives behind a repository interface.
SQLite (`app/store.py`) is the local default; Postgres
(`app/postgres_store.py`, schema in `app/postgres_schema.sql`)
is selected by `DATABASE_URL` — endpoint code does not change.

```
# local default: ./agentdocs.db
DATABASE_URL=sqlite:///./agentdocs.db

# production: Postgres (needs psycopg)
pip install agent-docs-api[postgres]   # or: pip install "psycopg[binary]>=3.1"
DATABASE_URL=postgresql://<service-account>@<host>:5432/agentdocs
```

The Postgres schema script is idempotent and is run by the
store on init. It can also be run by hand:
`psql "$DATABASE_URL" -f app/postgres_schema.sql`. It is a
fresh schema, not a migration: databases created under the old
schema (draft columns, version ids, inline HTML) are not
migrated — start from an empty database.

Credentials never live in code: `DATABASE_URL` carries the
already-governed DB service account, supplied by the service
tier. Agents and users never receive DB credentials.

## Version storage: S3 only

Published version HTML lives only in S3. A publish uploads the
HTML first and the version row holds only the object key
(`document_versions.s3_key`) plus the SHA-256. The database
stays the source of truth for which versions exist; S3 holds
only the bytes. Publishing without a blob store configured
fails, by design — there is no inline-HTML fallback.

```
pip install agent-docs-api[s3]   # or: pip install "boto3>=1.28"
AGENT_DOCS_S3_BUCKET=<bucket>
# optional, for S3-compatible storage / region:
AGENT_DOCS_S3_ENDPOINT_URL=<endpoint>
AGENT_DOCS_S3_REGION=<region>
```

Keys are `docs/<doc_id>/v<version_no>.html` — one key
per version, written once, never overwritten (a retried publish
after a version-number collision writes a new key; the orphan
from the failed attempt is harmless). The bucket is private and
the API's service account is its only reader: the API fetches
the bytes after its own checks and verifies them against the
stored SHA-256 before serving them, so the viewer never talks
to S3, never sees a bucket URL, and never receives a presigned
URL.

## Auth: placeholder seam only

The API accepts an `Authorization` header and passes it through as
an opaque `actor` string. It is **not verified against any identity
provider here**. The deployer wires real identity/authorization in
front of this service. Do not put real credentials in this repo.

The JWT token URL is `JWT_TOKEN_URL` in `app/main.py`, set in code
by the maintainer before deployment, matching the CLI's `TOKEN_URL`.
It is deliberately not a user-facing option or environment variable:
end users never configure it.

## Run

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main
# listens on http://127.0.0.1:8000 (PORT env overrides the port)
```

## Endpoints

- `POST /v1/documents` — create from `{title, html}`;
  stores version 1.
- `POST /v1/documents/{id}/versions` — publish a new version;
  body `{html}`. The next version number is
  `coalesce(max(version_no), 0) + 1`; there is no base version
  and no 409 — the `(document_id, version_no)` primary key
  serializes races (the loser recomputes and retries once).
- `GET /v1/documents` — list documents.
- `GET /v1/documents/{id}` — document with its latest version.
- `GET /v1/documents/{id}/versions` — version list (metadata only).
- `GET /v1/documents/{id}/versions/{number}` — one version's metadata.
- `GET /v1/documents/{id}/versions/{number}/html` — raw HTML,
  byte-identical to what was published.
- `GET /health` — liveness check.
- `GET /v1/documents/{id}/state` — all shared state for a document.
- `PUT /v1/documents/{id}/state/{key}` — set one key (`{"value": …}`;
  null deletes the key).
- `GET /v1/documents/{id}/state/{key}` — one key's value.
- `DELETE /v1/documents/{id}/state/{key}` — delete one key.
- `POST /v1/documents/{id}/state` — patch many keys at once
  (`{"patch": {…}}`; null values in the patch delete those keys).
- `POST /v1/usage` — live-call usage beacon: the viewer bridge
  reports each apiFetch here (fire-and-forget). Body
  `{document_id, version_no?, app_name, method, path,
  status_code?, error?, duration_ms}`; the actor comes from the
  Authorization header, never the body, and any query string in
  the path is stripped server-side. Bodies, headers, and tokens
  are never stored.

## Publish-time validation — the deterministic gate

Copilot instructions are guidance, not enforcement: an agent
can ignore them, and a publish via the CLI bypasses Copilot
entirely. So `POST /v1/documents` and
`POST /v1/documents/{id}/versions` both validate the HTML
before anything is stored, and a rejected publish leaves the
document and its version history untouched. Rejection is 400
`html_validation_failed` with a list of violation codes, so
the author fixes the exact problem and republishes:

- `secret_detected` — private key, bearer token, JWT, or a
  secret assignment (`api_key`, `client_secret`, `access_token`,
  `refresh_token`, `private_key`). Violation messages never
  echo matched content back.
- `token_url_in_html` — a token URL or `tokenUrl` identifier;
  token URLs live in the registry, never in a document.
- `direct_fetch` — direct `fetch()`, `XMLHttpRequest`, axios,
  or WebSocket; live data goes only via `apiFetch`.
- `external_resource` — external scripts, stylesheets,
  images, iframes, media, form actions, CSS imports, or JS
  module imports; documents are self-contained.
- `react_import` — React imports / `createElement`; documents
  are plain HTML.

Deliberately NOT rejected: `apiFetch` to an app that is not
registered yet, so pages that fall back to sample data (or a
403 notice) when an app is unregistered still publish. Plain
`<a href>` links are also allowed; only resource loads are
blocked.

## API registry — onboard an internal API once, use it in any document

Register an API's base URL and its per-endpoint token URL; the
registry stores URLs only, never tokens or secrets. Documents
refer to an API by app name, and the viewer resolves its token URL
at view time, so rotating a token URL needs no document
republishing and no viewer redeploy. The registry is per
deployment (one database per environment); there is no env
column, and app names must match across environment databases
or documents break on promotion.

- `POST /v1/apis` — register `{app_name, base_url, token_url?,
  swagger_url?}`; duplicate app_name returns 409, bad name or
  non-https URL returns 400. `token_url` is nullable: a missing
  token URL means no token fetch (apiKey/bearer schemes).
- `GET /v1/apis` — list registered APIs (`?user_id=…` filter;
  a lookup, not access control).
- Every registry entry is stamped with the registering `user_id`:
  the bearer token's value when one is supplied, `anonymous`
  when no token is present. This is a record for lookup and
  audit, not access control — no endpoint restricts an
  entry's update or delete to its registering user; that needs
  verified identity first.
- `GET /v1/apis/{app_name}` — one registered API.
- `PUT /v1/apis/{app_name}` — update `base_url` / `token_url` /
  `swagger_url`. An explicitly sent null clears a nullable
  column; an absent field keeps it.
- `DELETE /v1/apis/{app_name}` — remove an API.
- `POST /v1/apis/{app_name}/sync` — fetch the latest Swagger doc
  and update the app's registry entry. Body `{"swagger_url": …}`
  sets the stored swagger URL; with no body, the stored
  `swagger_url` is used, so a later sync needs no URL. Base URL
  comes from `servers[0].url`, endpoints from `paths`, token URL
  from the Swagger oauth2 `tokenUrl` if present else the existing
  token URL is kept (paths alone never invent a token URL; a
  missing token URL stays missing). Returns added / removed /
  unchanged endpoints; removed endpoints are reported, never
  auto-deleted. Every sync writes one `api_sync_log` row.
  Swagger JSON is supported; YAML needs PyYAML.

Before production, set `TRUSTED_TOKEN_URL_ORIGINS` in
`app/main.py` to the allowed token-URL origins; it is empty in
this build, meaning no origin restriction is enforced.

Versions are immutable and carry a SHA-256 of the HTML. There
is no document size cap in this API: HTML is stored as-is,
however large; storage is bounded only by the bucket.

## Tests

```
.venv/bin/python -m pytest -q
```
