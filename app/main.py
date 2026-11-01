"""agent-docs API: publish, view, version history, and shared state.

Documents are plain, self-contained HTML written by the agent.
Shared document state is a document-scoped key-value store
(SlashDocs-style): it survives across versions, changing it
never creates a version, and document JavaScript reads/writes
it through the viewer's bridge. No comments, reactions,
mentions, notifications, grants UI, or approvals UI exist in
this service.

Auth is a placeholder seam only: the caller may send an
Authorization header, whose value is stored as the registering
``user_id`` for audit. It is NOT verified against any identity
provider here. In the absence of a token, user_id is
``anonymous``. The deployer wires real identity/authorization
in front of this service; that integration is deliberately out
of this repo, and no identity provider is named or integrated here.
When verified identity lands, _user_id is where the verified
user id replaces this header passthrough — the stored user_id
column needs no schema change.

The JWT token URL is set in code below (JWT_TOKEN_URL), matching
the CLI's TOKEN_URL. It is NOT a user-facing option or env var:
the maintainer replaces it in code before deployment, and end
users never configure it.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .store import Store, get_store, validate_state_key, validate_state_value
from .validate_html import validate_document_html
from typing import Optional

# No document size cap, by design: a document's HTML is stored as-is,
# however large. Storage is bounded only by the database / disk, not
# by this API. A deployer who wants a limit should enforce it at the
# reverse proxy, not here.
# JWT token URL, set in code — not a user option, not an env var.
# MAINTAINER: replace this value with the real token URL before
# deployment, matching the CLI's TOKEN_URL. End users must never
# be asked to set it. Token verification itself is the deployer's
# wiring and is deliberately not implemented here.
JWT_TOKEN_URL = "REPLACE_ME_WITH_JWT_TOKEN_URL"

app = FastAPI(title="agent-docs API", version="0.1.0")
_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = get_store()
    return _store


def set_store(s: Store) -> None:
    """Test seam: replace the store with a test database."""
    global _store
    _store = s


def _user_id(authorization: str | None) -> str:
    # Placeholder seam only: pass the header value through as the
    # user id. Never verified here, never printed. No token ->
    # "anonymous", by design, until verified identity replaces this.
    if not authorization:
        return "anonymous"
    return authorization.removeprefix("Bearer ").strip() or "anonymous"


def _validate_html_or_400(html: str) -> None:
    """Publish-time validation gate (create + publish).

    Rejects with specific violation codes so the author or
    Copilot can fix the exact problem and republish. Runs
    before any version is stored, so a rejected publish leaves
    the document and its version history untouched.
    """
    violations = validate_document_html(html)
    if violations:
        raise HTTPException(
            status_code=400,
            detail={"error": "html_validation_failed", "violations": violations},
        )


def _version_dict(v) -> dict:
    return {
        "id": v.id,
        "document_id": v.document_id,
        "number": v.number,
        "sha256": v.sha256,
        "change_summary": v.change_summary,
        "created_at": v.created_at,
    }


class CreateDocumentRequest(BaseModel):
    title: str = Field(min_length=1)
    html: str = Field(min_length=1)
    change_summary: str = ""


class PublishVersionRequest(BaseModel):
    html: str = Field(min_length=1)
    base_version_id: str = Field(min_length=1)
    change_summary: str = ""


class SetStateRequest(BaseModel):
    # Any JSON value; null deletes the key (SlashDocs sentinel).
    value: object = None


class PatchStateRequest(BaseModel):
    patch: dict


class RegisterApiRequest(BaseModel):
    name: str = Field(min_length=1)
    api_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    scope: str = ""
    app: Optional[str] = None
    swagger_url: Optional[str] = None


class UpdateApiRequest(BaseModel):
    api_url: Optional[str] = None
    token_url: Optional[str] = None
    scope: Optional[str] = None
    app: Optional[str] = None
    swagger_url: Optional[str] = None


class SyncApiRequest(BaseModel):
    swagger_url: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/documents", status_code=201)
def create_document(
    req: CreateDocumentRequest,
    authorization: str | None = Header(default=None),
):
    _validate_html_or_400(req.html)
    doc, version = store().create_document(
        req.title, req.html, req.change_summary, _user_id(authorization)
    )
    return {"id": doc.id, "title": doc.title, "version": _version_dict(version)}


@app.get("/v1/documents")
def list_documents():
    return {"documents": store().list_documents()}


@app.get("/v1/documents/{doc_id}")
def get_document(doc_id: str):
    doc = store().get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    latest = store().latest_version(doc_id)
    return {
        "id": doc.id,
        "title": doc.title,
        "latest_version": _version_dict(latest) if latest else None,
    }


@app.post("/v1/documents/{doc_id}/versions", status_code=201)
def publish_version(
    doc_id: str,
    req: PublishVersionRequest,
    authorization: str | None = Header(default=None),
):
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    _validate_html_or_400(req.html)
    version, current = store().publish_version(
        doc_id, req.html, req.base_version_id, req.change_summary,
        _user_id(authorization),
    )
    if version is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "current_version_id": current,
            },
        )
    return {"version": _version_dict(version)}


@app.get("/v1/documents/{doc_id}/versions")
def list_versions(doc_id: str):
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"versions": [_version_dict(v) for v in store().list_versions(doc_id)]}


@app.get("/v1/documents/{doc_id}/versions/{version_id}")
def get_version(doc_id: str, version_id: str):
    version = store().get_version(doc_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    return {"version": _version_dict(version)}


@app.get("/v1/documents/{doc_id}/versions/{version_id}/html")
def get_version_html(doc_id: str, version_id: str):
    version = store().get_version(doc_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    # Byte-identical raw HTML. No wrapping, no rewriting.
    return Response(content=version.html, media_type="text/html")


# Trusted token-URL origins, set in code by the maintainer before
# production (empty = no origin restriction in this build, for
# local onboarding; production deployments should list the
# internal domains / Entra tenant allowed here and reject others).
TRUSTED_TOKEN_URL_ORIGINS: list[str] = []


def _check_token_url_origin(token_url: str) -> None:
    if not TRUSTED_TOKEN_URL_ORIGINS:
        return
    from urllib.parse import urlparse

    origin = urlparse(token_url).netloc
    if origin not in TRUSTED_TOKEN_URL_ORIGINS:
        raise HTTPException(status_code=400, detail="token_url_origin_not_trusted")


def _require_document(doc_id: str) -> None:
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")


def fetch_swagger_doc(url: str) -> dict:
    """Fetch an OpenAPI/Swagger doc (JSON, or YAML if PyYAML is installed).

    Patchable in tests: monkeypatch app.main.fetch_swagger_doc.
    """
    import httpx

    with httpx.Client(timeout=30, trust_env=False) as c:
        r = c.get(url)
        r.raise_for_status()
        text = r.text.strip()
    if text.startswith("{"):
        import json as _json

        return _json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError:
        raise HTTPException(status_code=400, detail="swagger_yaml_not_supported")
    return yaml.safe_load(text)


def _swagger_token_url(doc: dict) -> str | None:
    schemes = (doc.get("components") or {}).get("securitySchemes") or {}
    for scheme in schemes.values():
        flows = (scheme or {}).get("flows") or {}
        for flow_name in ("authorizationCode", "clientCredentials", "password", "implicit"):
            flow = flows.get(flow_name) or {}
            if flow.get("tokenUrl"):
                return flow["tokenUrl"]
    return None


@app.post("/v1/apis", status_code=201)
def register_api(
    req: RegisterApiRequest,
    authorization: str | None = Header(default=None),
):
    _check_token_url_origin(req.token_url)
    try:
        api = store().register_api(req.name, req.api_url, req.token_url, req.scope, _user_id(authorization), app=req.app, swagger_url=req.swagger_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if api is None:
        raise HTTPException(status_code=409, detail="api_already_registered")
    return {"api": api}


@app.get("/v1/apis")
def list_apis(app: str | None = None, user_id: str | None = None):
    """List registry entries, optionally filtered by app and/or user_id.

    Filtering is a record lookup, not access control: it does not
    restrict who may see an entry.
    """
    return {"apis": store().list_apis(app=app, user_id=user_id)}


@app.post("/v1/apis/{name}/sync")
def sync_api(name: str, req: SyncApiRequest, authorization: str | None = Header(default=None)):
    """Fetch the latest Swagger doc for an app and update its registry entry.

    The swagger_url is taken from the request if given, else from
    the app config stored in the registry — so a later sync needs
    no URL at all: `apis sync --app refdp`. Base URL comes from
    servers[0].url, endpoints from paths. Token URL comes from
    the Swagger oauth2 tokenUrl if present, else the existing
    token URL is kept (Swagger paths alone never invent one).
    Stale endpoints are reported, never auto-deleted.
    """
    existing = store().get_api(name)
    swagger_url = req.swagger_url or (existing or {}).get("swagger_url") or ""
    if not swagger_url:
        raise HTTPException(status_code=400, detail="swagger_url_not_configured")
    if existing is None and req.swagger_url is None:
        raise HTTPException(status_code=404, detail="api_not_found")
    try:
        doc = fetch_swagger_doc(swagger_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"swagger_fetch_failed: {e}")
    servers = doc.get("servers") or []
    base_url = (servers[0].get("url") if servers else "") or (existing or {}).get("api_url") or ""
    paths = sorted((doc.get("paths") or {}).keys())
    swagger_token_url = _swagger_token_url(doc)
    token_url = swagger_token_url or (existing or {}).get("token_url") or ""
    if not token_url:
        raise HTTPException(status_code=400, detail="token_url_not_found_in_swagger")
    _check_token_url_origin(token_url)
    import json as _json

    previous = _json.loads((existing or {}).get("endpoints") or "[]") if existing else []
    added = [p for p in paths if p not in previous]
    removed = [p for p in previous if p not in paths]
    unchanged = [p for p in paths if p in previous]
    if existing is None:
        try:
            api = store().register_api(name, base_url, token_url, "", _user_id(authorization), app=name, swagger_url=swagger_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        store().update_api(name, None, None, None, endpoints=paths)
        api = store().get_api(name)
    else:
        token_changed = token_url != existing["token_url"]
        api = store().update_api(name, base_url, token_url, None, swagger_url=swagger_url, endpoints=paths)
        token_changed = token_changed
    return {
        "api": api,
        "swagger_url": swagger_url,
        "base_url": base_url,
        "token_url_changed": (swagger_token_url is not None and existing is not None and swagger_token_url != existing["token_url"]),
        "added_endpoints": added,
        "removed_endpoints": removed,
        "unchanged_endpoints": unchanged,
    }


@app.get("/v1/apis/{name}")
def get_api(name: str):
    api = store().get_api(name)
    if api is None:
        raise HTTPException(status_code=404, detail="api_not_found")
    return {"api": api}


@app.put("/v1/apis/{name}")
def update_api(name: str, req: UpdateApiRequest):
    _check_token_url_origin(req.token_url) if req.token_url else None
    try:
        api = store().update_api(name, req.api_url, req.token_url, req.scope, app=req.app, swagger_url=req.swagger_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if api is None:
        raise HTTPException(status_code=404, detail="api_not_found")
    return {"api": api}


@app.delete("/v1/apis/{name}")
def delete_api(name: str):
    return {"name": name, "deleted": store().delete_api(name)}


@app.get("/v1/documents/{doc_id}/state")
def get_state(doc_id: str):
    _require_document(doc_id)
    return {"document_id": doc_id, "state": store().get_state(doc_id)}


@app.post("/v1/documents/{doc_id}/state")
def patch_state(
    doc_id: str,
    req: PatchStateRequest,
    authorization: str | None = Header(default=None),
):
    _require_document(doc_id)
    try:
        state = store().patch_state(doc_id, req.patch, _user_id(authorization))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"document_id": doc_id, "state": state}


@app.put("/v1/documents/{doc_id}/state/{key}")
def set_state(
    doc_id: str,
    key: str,
    req: SetStateRequest,
    authorization: str | None = Header(default=None),
):
    _require_document(doc_id)
    try:
        kept = store().set_state_value(doc_id, key, req.value, _user_id(authorization))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"document_id": doc_id, "key": key, "deleted": not kept}


@app.get("/v1/documents/{doc_id}/state/{key}")
def get_state_value(doc_id: str, key: str):
    _require_document(doc_id)
    try:
        validate_state_key(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    value = store().get_state_value(doc_id, key)
    if value is None:
        raise HTTPException(status_code=404, detail="state_key_not_found")
    return {"document_id": doc_id, "key": key, "value": value}


@app.delete("/v1/documents/{doc_id}/state/{key}")
def delete_state(doc_id: str, key: str):
    _require_document(doc_id)
    try:
        deleted = store().delete_state_value(doc_id, key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"document_id": doc_id, "key": key, "deleted": deleted}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
