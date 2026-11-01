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

## Database: SQLite now, Postgres as the target

All database access lives in `app/store.py` behind the `Store`
repository class. The current backend is SQLite, selected by
`DATABASE_URL`:

```
# default: ./agentdocs.db
DATABASE_URL=sqlite:///./agentdocs.db

# tests / ephemeral
DATABASE_URL=sqlite:///:memory:
```

A Postgres URL today raises an explicit error instead of being
silently ignored. The Postgres target is a new `Store` implementation
behind the same interface, selected from the same `DATABASE_URL` —
endpoint code does not change.

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
