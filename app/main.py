"""agent-docs API: publish, view, and version history only.

Documents are plain, self-contained HTML written by the agent.
No comments, reactions, mentions, notifications, shared state,
grants UI, or approvals UI exist in this service.

Auth is a placeholder seam only: the caller may send an
Authorization header, which is passed through as ``actor`` and
stored for audit. It is NOT verified against any identity
provider here. The deployer wires real identity/authorization
in front of this service; that integration is deliberately out
of this repo, and no identity provider is named or integrated here.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .store import Store, get_store

MAX_HTML_BYTES = 10 * 1024 * 1024

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


def _actor(authorization: str | None) -> str:
    # Placeholder seam only: pass the header value through as an
    # opaque actor string. Never verified here, never printed.
    if not authorization:
        return "anonymous"
    return authorization.removeprefix("Bearer ").strip() or "anonymous"


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


def _check_html_size(html: str) -> None:
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="html_too_large")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/documents", status_code=201)
def create_document(
    req: CreateDocumentRequest,
    authorization: str | None = Header(default=None),
):
    _check_html_size(req.html)
    doc, version = store().create_document(
        req.title, req.html, req.change_summary, _actor(authorization)
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
    _check_html_size(req.html)
    if store().get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    version, current = store().publish_version(
        doc_id, req.html, req.base_version_id, req.change_summary,
        _actor(authorization),
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


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
