"""agent-docs API: publish, view, version history, shared state,
registry, and the live-call usage log.

Documents are plain, self-contained HTML written by the agent.
Publishing appends the next version — there are no drafts, no
approvals, no version ids, and no base-version conflicts: the
next version number is coalesce(max(version_no), 0) + 1, and the
(document_id, version_no) primary key is the concurrency
control. The viewer resolves a version from the URL: no version
supplied means latest; an explicit version resolves exactly or
404s, never silently falling back.

Version content lives ONLY in S3 (see app/blob_store.py): the
row holds s3_key + content_sha256. The API serves bytes from
S3 after verifying the hash; the viewer never talks to S3.

Shared document state is a document-scoped key-value store
(SlashDocs-style): it survives across versions, changing it
never creates a version, and document JavaScript reads/writes
it through the viewer's bridge. No comments, reactions,
mentions, notifications, or grants exist in this service.

The registry is per deployment (one database per environment);
there is no env column. app_name is the whole identity.
user_id is audit only, never access control.

The usage log (POST /v1/usage) is the audit feed for live
calls: the viewer bridge reports each apiFetch here,
fire-and-forget. It never holds query strings, bodies,
headers, or tokens.

Auth is a placeholder seam only: the caller may send an
Authorization header, whose value is stored as the registering
``user_id`` for audit. It is NOT verified against any identity
provider here. In the absence of a token, user_id is
``anonymous``. The deployer wires real identity/authorization
in front of this service; that integration is deliberately out
of this repo, and no identity provider is named or integrated here.
When verified identity lands, _user_id is where the verified
user id replaces this header passthrough.

The JWT token URL is set in code below (JWT_TOKEN_URL), matching
the CLI's TOKEN_URL. It is NOT a user-facing option or env var:
the maintainer replaces it in code before deployment, and end
users never configure it.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .store import (
    _UNSET,
    _USAGE_METHODS,
    Store,
    get_store,
    validate_state_key,
    validate_state_value,
)
from .validate_html import validate_document_html
from typing import Optional

# No document size cap, by design: a document's HTML is stored as-is,
# however large. Storage is bounded only by the bucket / database,
# not by this API. A deployer who wants a limit should enforce it at
# the reverse proxy, not here.
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
    # A store that already carries a blob store (tests, or a
    # deployer that attached one) is left alone; otherwise attach
    # one from the environment. Publishing without a blob store
    # fails: version content is S3-only.
    if getattr(_store, "blob_store", None) is None:
        from .blob_store import blob_store_from_env

        blob = blob_store_from_env()
        if blob is not None:
            _store.blob_store = blob
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
        "document_id": v.document_id,
        "number": v.number,
        "sha256": v.sha256,
        "s3_key": v.s3_key,
        "created_at": v.created_at,
    }


def _resolve_version_html(version) -> str:
    """Version HTML, fetched from S3 by the API (never the viewer)
    and verified against the stored SHA-256 before being served,
    so a corrupt or swapped object can never be served as a
    version. A missing object, a missing blob store, or a hash
    mismatch is a server inconsistency (500), not a missing
    version (404) — the version row itself exists.
    """
    blob = getattr(store(), "blob_store", None)
    if blob is None:
        raise HTTPException(status_code=500, detail="blob_store_not_configured")
    data = blob.get(version.s3_key)
    if data is None:
        raise HTTPException(status_code=500, detail="version_blob_not_found")
    if hashlib.sha256(data).hexdigest() != version.sha256:
        raise HTTPException(status_code=500, detail="version_blob_hash_mismatch")
    return data.decode("utf-8")


def _audit(entity_type: str, entity_id: str, action: str, actor: str, details: dict | None = None) -> None:
    """Append one hash-chained audit entry. A failed audit write
    surfaces as a 500: audit is first-class here, and a mutation
    that cannot be recorded must not report success silently."""
    store().log_audit(entity_type, entity_id, action, actor, details or {})


class CreateDocumentRequest(BaseModel):
    title: str = Field(min_length=1)
    html: str = Field(min_length=1)


class PublishVersionRequest(BaseModel):
    html: str = Field(min_length=1)


class SetStateRequest(BaseModel):
    # Any JSON value; null deletes the key (SlashDocs sentinel).
    value: object = None


class PatchStateRequest(BaseModel):
    patch: dict


class RegisterApiRequest(BaseModel):
    app_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    # Nullable: a missing token URL means no token fetch
    # (apiKey/bearer schemes). Never a secret value.
    token_url: Optional[str] = None
    swagger_url: Optional[str] = None


class UpdateApiRequest(BaseModel):
    base_url: Optional[str] = None
    token_url: Optional[str] = None
    swagger_url: Optional[str] = None


class SyncApiRequest(BaseModel):
    swagger_url: Optional[str] = None


class UsageRequest(BaseModel):
    document_id: str = Field(min_length=1)
    version_no: Optional[int] = None
    app_name: str = Field(min_length=1)
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    status_code: Optional[int] = None
    error: Optional[str] = None
    duration_ms: int = Field(ge=0)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/documents", status_code=201)
def create_document(
    req: CreateDocumentRequest,
    authorization: str | None = Header(default=None),
):
    _validate_html_or_400(req.html)
    actor = _user_id(authorization)
    try:
        doc, version = store().create_document(req.title, req.html, actor)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    _audit("document", doc.id, "created", actor, {"title": doc.title, "version_no": 1})
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
    """Append the next version. No base version, no 409: the
    (document_id, version_no) primary key serializes races."""
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    _validate_html_or_400(req.html)
    actor = _user_id(authorization)
    try:
        version = store().publish_version(doc_id, req.html, actor)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    _audit("document", doc_id, "published", actor, {"version_no": version.number})
    return {"version": _version_dict(version)}


@app.get("/v1/documents/{doc_id}/versions")
def list_versions(doc_id: str):
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"versions": [_version_dict(v) for v in store().list_versions(doc_id)]}


@app.get("/v1/documents/{doc_id}/versions/{number}")
def get_version(doc_id: str, number: int):
    version = store().get_version(doc_id, number)
    if version is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    return {"version": _version_dict(version)}


@app.get("/v1/documents/{doc_id}/versions/{number}/html")
def get_version_html(doc_id: str, number: int):
    version = store().get_version(doc_id, number)
    if version is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    # Byte-identical raw HTML. No wrapping, no rewriting.
    # Served by the API from S3 — the viewer never talks to S3
    # and never sees a bucket URL.
    return Response(content=_resolve_version_html(version), media_type="text/html")


# Trusted token-URL origins, set in code by the maintainer before
# production (empty = no origin restriction in this build, for
# local onboarding; production deployments should list the
# internal domains / Entra tenant allowed here and reject others).
TRUSTED_TOKEN_URL_ORIGINS: list[str] = []


def _check_token_url_origin(token_url: str | None) -> None:
    if not token_url or not TRUSTED_TOKEN_URL_ORIGINS:
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
    actor = _user_id(authorization)
    try:
        api = store().register_api(
            req.app_name, req.base_url, req.token_url, actor,
            swagger_url=req.swagger_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if api is None:
        raise HTTPException(status_code=409, detail="api_already_registered")
    _audit("registry", req.app_name, "registered", actor, {"base_url": req.base_url})
    return {"api": api}


@app.get("/v1/apis")
def list_apis(user_id: str | None = None):
    """List registry entries, optionally filtered by user_id.

    Filtering is a record lookup, not access control: it does not
    restrict who may see an entry.
    """
    return {"apis": store().list_apis(user_id=user_id)}


@app.post("/v1/apis/{app_name}/sync")
def sync_api(app_name: str, req: SyncApiRequest, authorization: str | None = Header(default=None)):
    """Fetch the latest Swagger doc for an app and update its registry entry.

    The swagger_url is taken from the request if given, else from
    the entry stored in the registry — so a later sync needs
    no URL at all: `apis sync --app refdp`. Base URL comes from
    servers[0].url, endpoints from paths. Token URL comes from
    the Swagger oauth2 tokenUrl if present, else the existing
    token URL is kept (Swagger paths alone never invent one).
    A missing token URL stays missing: it means no token fetch.
    Stale endpoints are reported, never auto-deleted. Every sync
    writes one api_sync_log row.
    """
    existing = store().get_api(app_name)
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
    base_url = (servers[0].get("url") if servers else "") or (existing or {}).get("base_url") or ""
    paths = sorted((doc.get("paths") or {}).keys())
    swagger_token_url = _swagger_token_url(doc)
    token_url = swagger_token_url or (existing or {}).get("token_url")
    _check_token_url_origin(token_url)
    import json as _json

    spec_sha256 = hashlib.sha256(
        _json.dumps(doc, sort_keys=True).encode("utf-8")
    ).hexdigest()
    previous = (existing or {}).get("endpoints") or []
    added = [p for p in paths if p not in previous]
    removed = [p for p in previous if p not in paths]
    unchanged = [p for p in paths if p in previous]
    actor = _user_id(authorization)
    if existing is None:
        try:
            api = store().register_api(
                app_name, base_url, token_url, actor, swagger_url=swagger_url
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if api is None:
            raise HTTPException(status_code=409, detail="api_already_registered")
        store().update_api(app_name, endpoints=paths)
        api = store().get_api(app_name)
    else:
        api = store().update_api(
            app_name, base_url, token_url, swagger_url=swagger_url, endpoints=paths
        )
    store().log_sync(app_name, spec_sha256, len(added), removed, actor)
    _audit("registry", app_name, "synced", actor, {"added": len(added), "removed": removed})
    return {
        "api": api,
        "swagger_url": swagger_url,
        "base_url": base_url,
        "token_url_changed": (
            swagger_token_url is not None
            and existing is not None
            and swagger_token_url != existing["token_url"]
        ),
        "added_endpoints": added,
        "removed_endpoints": removed,
        "unchanged_endpoints": unchanged,
    }


@app.get("/v1/apis/{app_name}")
def get_api(app_name: str):
    api = store().get_api(app_name)
    if api is None:
        raise HTTPException(status_code=404, detail="api_not_found")
    return {"api": api}


@app.put("/v1/apis/{app_name}")
def update_api(
    app_name: str,
    req: UpdateApiRequest,
    authorization: str | None = Header(default=None),
):
    _check_token_url_origin(req.token_url) if req.token_url else None
    sent = req.model_fields_set
    try:
        api = store().update_api(
            app_name,
            base_url=req.base_url if "base_url" in sent else _UNSET,
            # Explicit null clears a nullable column; absent keeps it.
            token_url=req.token_url if "token_url" in sent else _UNSET,
            swagger_url=req.swagger_url if "swagger_url" in sent else _UNSET,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if api is None:
        raise HTTPException(status_code=404, detail="api_not_found")
    _audit("registry", app_name, "updated", _user_id(authorization), {})
    return {"api": api}


@app.delete("/v1/apis/{app_name}")
def delete_api(
    app_name: str,
    authorization: str | None = Header(default=None),
):
    deleted = store().delete_api(app_name)
    if deleted:
        _audit("registry", app_name, "deleted", _user_id(authorization), {})
    return {"app_name": app_name, "deleted": deleted}


@app.post("/v1/usage", status_code=201)
def log_usage(req: UsageRequest, authorization: str | None = Header(default=None)):
    """Live-call usage beacon. The viewer bridge reports each
    apiFetch here, fire-and-forget: the bridge ignores beacon
    failures, so this endpoint must never break a document —
    it only inserts a row.

    Actor is derived server-side from the Authorization header,
    never from a client-supplied field. Any query string in the
    path is stripped before storing, server-side, even if the
    bridge already stripped it. Bodies, headers, and tokens are
    never accepted here, let alone stored.
    """
    if req.method not in _USAGE_METHODS:
        raise HTTPException(status_code=400, detail="invalid_usage_method")
    # Defense in depth: the bridge strips query strings, but a
    # beacon carrying one must not let it into the log.
    path = urlsplit(req.path).path
    store().log_usage(
        req.document_id,
        req.version_no,
        req.app_name,
        req.method,
        path,
        req.status_code,
        req.error,
        req.duration_ms,
        _user_id(authorization),
    )
    return {"logged": True}


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
