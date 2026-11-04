# Canvas — About

> **Agents publish plain, self-contained HTML. People view it. Every version is kept.**

Canvas is a governed publication and audit layer for agent-built documents.
An agent writes a document once, publishes it, and the link stays durable
while new versions stack underneath it. Nothing in a document is a framework
component, and no document ever carries a secret.

If you have two minutes, start at [Try it](#try-it-in-2-minutes). If you have
thirty seconds, read the table below — that is the whole product.

## At a glance

| Who | What they do | What they get |
| --- | --- | --- |
| Agent (via MCP or CLI) | Publishes one self-contained HTML file | A durable document id, version 1 |
| Agent again | Publishes the same file, updated | Version 2, 3, 4… — v1 is untouched |
| Person (in the viewer) | Opens the document link | The latest version, or any pinned version by number |
| Document itself, at view time | Calls a registered internal API through the viewer bridge | Fresh data, without a token in the HTML |

```
Agent ── publish HTML ──► Canvas API ── store version ──► S3 (HTML bytes)
                                                        Postgres (metadata)
Person ── open link ────► Viewer ─────► Canvas API ──► latest, or version N
Document ── live data ──► Viewer bridge (apiFetch) ──► registered API
```

## The three rules that explain everything else

1. **Documents are plain HTML, never React.** React is viewer chrome only,
   in the UI repo. A document with an external script, stylesheet, image,
   iframe, React import, or secret is rejected at publish time — and a
   rejected publish stores nothing.
2. **Versions are numbers, and they are immutable.** Publish appends the
   next number (`max + 1`). No version in the URL means latest. An explicit
   version means exactly that version, or 404 — never a silent fallback.
3. **Shared state is separate from versions.** Each document has a small
   key-value store. Changing state never creates a version, and publishing
   never resets state. Setting a key to `null` deletes it.

## Try it in 2 minutes

Run the API (from this repo's README — FastAPI, port 8000 by default):

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main
# listens on http://127.0.0.1:8000
```

Then, with `curl`:

```bash
# 1. Health check
curl http://127.0.0.1:8000/health

# 2. Create a document — this stores version 1 in one call
curl -X POST http://127.0.0.1:8000/v1/documents \
  -H 'Content-Type: application/json' \
  -d '{"title": "Hello Canvas", "html": "<h1>Hello Canvas</h1>"}'
# → {"id": "doc_…", …}  — keep the id

# 3. Publish version 2 — body is HTML only, no base version, no summary
curl -X POST http://127.0.0.1:8000/v1/documents/doc_…/versions \
  -H 'Content-Type: application/json' \
  -d '{"html": "<h1>Hello Canvas, v2</h1>"}'

# 4. Read it back — latest, and pinned
curl http://127.0.0.1:8000/v1/documents/doc_…/versions/1/html
curl http://127.0.0.1:8000/v1/documents/doc_…/versions/2/html
curl http://127.0.0.1:8000/v1/documents/doc_…/versions/99/html
# → 404. A missing version is a 404, never the latest in disguise.
```

## The API, at a glance

Base URL: `http://127.0.0.1:8000` locally. All endpoints in this repo are
unauthenticated for now (see [Auth](#auth-status) below).

| Area | Endpoint | What it does |
| --- | --- | --- |
| Health | `GET /health` | Liveness check |
| Documents | `POST /v1/documents` | Create a document + version 1 (`{title, html}`) |
| Documents | `GET /v1/documents` · `GET /v1/documents/{doc_id}` | List · latest version of one document |
| Versions | `POST /v1/documents/{doc_id}/versions` | Publish next version (`{html}` only) |
| Versions | `GET …/versions` · `GET …/versions/{number}` · `GET …/versions/{number}/html` | Version list · one version's metadata · its raw HTML, byte-identical |
| Shared state | `GET /v1/documents/{doc_id}/state` | All keys for a document |
| Shared state | `PUT …/state/{key}` · `GET …/state/{key}` · `DELETE …/state/{key}` | Set one key (`{"value": …}`) · read one key · delete one key |
| Shared state | `POST /v1/documents/{doc_id}/state` | Patch several keys at once (`{"patch": {…}}`) |
| API registry | `POST /v1/apis` · `GET /v1/apis` | Register an internal API · list registered APIs |
| API registry | `GET /v1/apis/{app_name}` · `PUT …` · `DELETE …` | One API · update it · remove it |
| API registry | `POST /v1/apis/{app_name}/sync` | Sync from the app's Swagger spec (owner-triggered; removed endpoints are reported, never auto-deleted) |
| Usage | `POST /v1/usage` | Viewer bridge beacon: which live call happened, for audit. No query strings, bodies, headers, or tokens are stored. |

Full endpoint, storage, and validation detail lives in this repo's
[`README.md`](README.md) and in the build instructions at
[`seenow/copilot-instructions.md`](seenow/copilot-instructions.md).

### Storage, in one paragraph

Version HTML lives only in S3, in a private bucket, one key per version
(`docs/<doc_id>/v<version_no>.html`), written once and never overwritten.
Postgres holds metadata only: which documents and versions exist, the
SHA-256 of each version's bytes (verified before serving), the API
registry, shared state, and a hash-chained audit log. The viewer never
talks to S3 and never sees a bucket URL. Local dev uses SQLite with the
same semantics; production uses Postgres (`DATABASE_URL`).

### Live data, in one paragraph

Documents never call an API directly — no `fetch()`, no axios, no
WebSocket in published HTML. At view time, document JavaScript calls the
viewer's bridge (`window.agentdocs.apiFetch(app, path)`), and the viewer
resolves the app's registered API. Register an API once by app name
(`app_name`, `base_url`, optional `token_url` / `swagger_url`); documents
refer to it by name, so rotating a URL needs no republishing. The registry
stores URLs only — never tokens or secrets.

## Integrate with MCP — placeholder

> **PLACEHOLDER — fill in the server command when the Canvas MCP server
> ships.** The tool surface below is the planned one, from the Canvas MCP
> build instructions. Do not guess a package name or command until then.

Planned shape, for an MCP client (Copilot, Claude Code, Cursor-style agents):

```json
{
  "mcpServers": {
    "canvas": {
      "command": "<canvas-mcp-command — TBD>",
      "env": {
        "CANVAS_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

Planned tools — the same surface as the API, nothing invented:

- Documents/versions (by number only): `list_documents`, `get_document`,
  `create_document` (title + HTML → version 1), `publish_document`
  (HTML only → next version), `list_versions`, `get_version`,
  `get_version_html`.
- Shared state: `get_document_state`, `set_document_state` (null deletes),
  `patch_document_state`, `delete_document_state`.
- Registry: `register_api`, `list_apis`, `get_api`, `update_api`,
  `delete_api`, `sync_apis_from_swagger`.

Agent rules that matter: publish first, then view. Read state before
overwriting it (state survives versions). Address versions by number; a
missing version is a 404, not a fallback. If a publish is rejected with
`html_validation_failed`, fix the HTML (remove the secret, token URL,
external resource, or React import) and republish — nothing was stored.

## Install the command line — placeholder

> **PLACEHOLDER — fill in the install command when the Canvas CLI ships.**
> The command surface below is the planned one, from the Canvas CLI build
> instructions. Do not guess a package name until then. The `canvas`
> prefix below is illustrative — confirm the real binary name when the
> package ships.

```bash
# PLACEHOLDER — package name TBD
pip install <canvas-cli — TBD>

export CANVAS_API_URL="http://127.0.0.1:8000"  # default if unset

# Documents
canvas docs create --title "Hello Canvas" --html-file hello.html
canvas docs list
canvas docs get <doc_id> [--version N]
canvas docs publish <doc_id> --html-file hello-v2.html
canvas docs versions <doc_id>

# Shared state (never creates a version)
canvas state get <doc_id> [key]
canvas state set <doc_id> <key> <json-value>
canvas state patch <doc_id> <json-patch>
canvas state delete <doc_id> <key>

# API registry
canvas apis register <app_name> --base-url <url> [--token-url <url>] [--swagger-url <url>]
canvas apis list | canvas apis get <app_name> | canvas apis sync <app_name>
```

## The four repos

Canvas is four separate repos that talk over HTTP and never import each
other's code. This repo is the API only.

| Repo | What it is | Start here |
| --- | --- | --- |
| `agent-docs-api` (this repo) | FastAPI backend: publish, view, versions, state, registry, usage, audit | This page, then `README.md` |
| `agent-docs-ui` | React viewer chrome + sandboxed iframe; documents are plain HTML | Its README, then its `seenow/` instructions |
| `agent-docs-cli` | Thin command-line client over this API | The placeholder above, then its `seenow/` instructions |
| `agent-docs-mcp` | Thin MCP tool layer over this API, for Copilot-style agents | The placeholder above, then its `seenow/` instructions |

Each repo carries its own from-scratch build instructions under
`seenow/copilot-instructions.md`.

## What Canvas is not

- Not a wiki, not a docs site generator, not a CMS: no pages tree, no
  templates, no markdown rendering pipeline. One self-contained HTML file
  per version.
- Not a collaboration tool: no comments, reactions, mentions,
  notifications, grants, sharing UI, co-editing, or presence.
- Not a draft/approval system: no drafts, no approval gate, no
  creator-approver checks. Publish appends the next version directly.
- Not a general API catalog: the registry is one app at a time, onboarded
  by name, for documents to call at view time — not a browsable SaaS
  directory.

## Auth status

**Everything is unauthenticated for now, by explicit decision.** Do not
build login, token verification, OAuth, session cookies, or RBAC into
Canvas yet — the maintainer will wire authentication later and will say
so explicitly when that changes. Until then, audit, registry, and usage
records use one fixed actor: `anonymous`. Never put a secret, token, or
credential in code, logs, test fixtures, error responses, or published
HTML — the registry stores URLs only, and in unauthenticated mode a stored
`token_url` is never used for a fetch.
