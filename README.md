# agent-docs-api

Python (FastAPI) backend for agent-docs. Separate repo — it does not
import code from the cli, mcp, or ui repos, and they do not import
code from it. They talk to it over HTTP.

## Scope — publish, view, version history, and shared state

- Agents publish plain, self-contained HTML documents.
- Humans view documents and any past version.
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
store on init, so it also migrates a database created before
drafts / app grouping / user_id existed. It can also be run
by hand: `psql "$DATABASE_URL" -f app/postgres_schema.sql`.

Credentials never live in code: `DATABASE_URL` carries the
already-governed DB service account, supplied by the service
tier. Agents and users never receive DB credentials.

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

- `POST /v1/documents` — create from `{title, html, change_summary?}`;
  stores version 1.
- `POST /v1/documents/{id}/versions` — publish a new version;
  body `{html, base_version_id, change_summary?}`. A stale
  `base_version_id` returns 409 with `current_version_id`.
- `GET /v1/documents` — list documents.
- `GET /v1/documents/{id}` — document with its latest version.
- `GET /v1/documents/{id}/versions` — version list (metadata only).
- `GET /v1/documents/{id}/versions/{version_id}` — one version's metadata.
- `GET /v1/documents/{id}/versions/{version_id}/html` — raw HTML,
  byte-identical to what was published.
- `GET /health` — liveness check.
- `GET /v1/documents/{id}/state` — all shared state for a document.
- `PUT /v1/documents/{id}/state/{key}` — set one key (`{"value": …}`;
  null deletes the key).
- `GET /v1/documents/{id}/state/{key}` — one key's value.
- `DELETE /v1/documents/{id}/state/{key}` — delete one key.
- `POST /v1/documents/{id}/state` — patch many keys at once
  (`{"patch": {…}}`; null values in the patch delete those keys).

## Drafts — test on the actual viewer link before publishing

A draft is an unpublished HTML snapshot for a document. It
creates no version, appears in no version list, and saving one
touches no published version. It is rendered in the viewer
through the same bridge as a published version, so live data,
state, and navigation all work while testing.

- `POST /v1/documents` with `{"draft": true}` — create a
  draft-only document (no version yet).
- `PUT /v1/documents/{id}/draft` — save/replace a draft.
  Validated exactly like a publish; a rejected draft stores
  nothing.
- `GET /v1/documents/{id}/draft` — the stored draft HTML.
- `DELETE /v1/documents/{id}/draft` — discard a draft.
- `POST /v1/documents/{id}/draft/publish` — publish the draft
  as the next version (version 1 for a draft-only document);
  clears the draft. A direct publish also clears any draft.
- Viewer link: `?doc=<id>&draft=1`. The document list and
  `GET /v1/documents/{id}` report `has_draft`.

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
refer to an API by name, and the viewer resolves its token URL
at view time, so rotating a token URL needs no document
republishing and no viewer redeploy.

- `POST /v1/apis` — register `{name, api_url, token_url, scope?,
  app?, swagger_url?}`; duplicate name returns 409, bad name or
  non-https URL returns 400. `name` is the app short name
  (e.g. `refdp`); `app` defaults to `name` if not given.
- `GET /v1/apis` — list registered APIs (`?app=refdp` and
  `?user_id=…` filters).
- Every registry entry is stamped with the registering `user_id`:
  the bearer token's value when one is supplied, `anonymous`
  when no token is present. This is a record for lookup and
  audit, not access control — no endpoint yet restricts an
  entry's update or delete to its registering user; that needs
  verified identity first.
- `GET /v1/apis/{name}` — one registered API.
- `PUT /v1/apis/{name}` — update `api_url` / `token_url` / `scope` /
  `app` / `swagger_url`.
- `DELETE /v1/apis/{name}` — remove an API.
- `POST /v1/apis/{name}/sync` — fetch the latest Swagger doc
  and update the app's registry entry. Body `{"swagger_url": …}`
  sets the stored swagger URL; with no body, the stored
  `swagger_url` from the app config is used, so a later sync
  needs no URL. Base URL comes from `servers[0].url`, endpoints
  from `paths`, token URL from the Swagger oauth2 `tokenUrl`
  if present else the existing token URL is kept (paths alone
  never invent a token URL). Returns added / removed /
  unchanged endpoints; removed endpoints are reported, never
  auto-deleted. Swagger JSON is supported; YAML needs PyYAML.

Before production, set `TRUSTED_TOKEN_URL_ORIGINS` in
`app/main.py` to the allowed token-URL origins; it is empty in
this build, meaning no origin restriction is enforced.

Versions are immutable, carry a SHA-256 of the HTML, and a
change summary. There is no document size cap in this API:
HTML is stored as-is, however large; storage is bounded only
by the database / disk.

## Tests

```
.venv/bin/python -m pytest -q
```
