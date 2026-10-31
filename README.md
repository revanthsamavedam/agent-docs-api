# agent-docs-api

Python (FastAPI) backend for agent-docs. Separate repo — it does not
import code from the cli, mcp, or ui repos, and they do not import
code from it. They talk to it over HTTP.

## Scope — publish, view, and version history only

- Agents publish plain, self-contained HTML documents.
- Humans view documents and any past version.
- No comments, reactions, mentions, notifications, shared state,
  grants UI, or approvals UI.

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

Versions are immutable, carry a SHA-256 of the HTML, and a
change summary. HTML larger than 10 MiB is rejected (413).

## Tests

```
.venv/bin/python -m pytest -q
```
