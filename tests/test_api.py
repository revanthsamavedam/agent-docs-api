"""API tests: publish, view, version history, and shared state."""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app, set_store
from app.store import Store

HTML_V1 = "<!doctype html><html><body><h1>Report v1</h1></body></html>"
HTML_V2 = "<!doctype html><html><body><h1>Report v2</h1></body></html>"


@pytest.fixture()
def client(tmp_path):
    set_store(Store(f"sqlite:///{tmp_path}/test.db"))
    with TestClient(app) as c:
        yield c
    set_store(None)


def _create(client):
    r = client.post(
        "/v1/documents",
        json={"title": "Q3 report", "html": HTML_V1, "change_summary": "v1"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_get(client):
    created = _create(client)
    doc_id = created["id"]
    assert created["version"]["number"] == 1
    assert len(created["version"]["sha256"]) == 64

    docs = client.get("/v1/documents").json()["documents"]
    assert [d["id"] for d in docs] == [doc_id]
    assert docs[0]["latest_version"] == 1

    got = client.get(f"/v1/documents/{doc_id}").json()
    assert got["title"] == "Q3 report"
    assert got["latest_version"]["number"] == 1


def test_publish_requires_current_base_and_old_version_is_immutable(client):
    created = _create(client)
    doc_id = created["id"]
    v1_id = created["version"]["id"]

    r = client.post(
        f"/v1/documents/{doc_id}/versions",
        json={"html": HTML_V2, "base_version_id": v1_id, "change_summary": "v2"},
    )
    assert r.status_code == 201, r.text
    v2_id = r.json()["version"]["id"]
    assert r.json()["version"]["number"] == 2

    # Stale base -> 409 with the current version id.
    stale = client.post(
        f"/v1/documents/{doc_id}/versions",
        json={"html": HTML_V2, "base_version_id": v1_id},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version_id"] == v2_id

    # Old version HTML is byte-identical and unchanged by the publish.
    html_v1 = client.get(f"/v1/documents/{doc_id}/versions/{v1_id}/html")
    assert html_v1.text == HTML_V1
    assert html_v1.headers["content-type"].startswith("text/html")
    html_v2 = client.get(f"/v1/documents/{doc_id}/versions/{v2_id}/html")
    assert html_v2.text == HTML_V2

    versions = client.get(f"/v1/documents/{doc_id}/versions").json()["versions"]
    assert [v["number"] for v in versions] == [1, 2]


def test_shared_state_is_document_scoped_not_version_scoped(client):
    created = _create(client)
    doc_id = created["id"]
    v1_id = created["version"]["id"]

    r = client.put(f"/v1/documents/{doc_id}/state/checklist.evidence_verified", json={"value": True})
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/documents/{doc_id}/state", json={"patch": {"approval.status": "approved", "gone": None}})
    assert r.status_code == 200, r.text
    state = client.get(f"/v1/documents/{doc_id}/state").json()["state"]
    assert state == {"approval.status": "approved", "checklist.evidence_verified": True}

    # Publish v2: state survives, and changing state creates no version.
    client.post(
        f"/v1/documents/{doc_id}/versions",
        json={"html": HTML_V2, "base_version_id": v1_id, "change_summary": "v2"},
    )
    assert client.get(f"/v1/documents/{doc_id}/state").json()["state"] == state
    assert len(client.get(f"/v1/documents/{doc_id}/versions").json()["versions"]) == 2

    # Null PUT deletes; bad key is rejected; missing doc is 404.
    assert client.put(f"/v1/documents/{doc_id}/state/approval.status", json={"value": None}).json()["deleted"] is True
    assert client.get(f"/v1/documents/{doc_id}/state/approval.status").status_code == 404
    assert client.put(f"/v1/documents/{doc_id}/state/bad key!", json={"value": 1}).status_code == 400
    assert client.get("/v1/documents/doc_missing/state").status_code == 404


def test_not_found(client):
    assert client.get("/v1/documents/doc_missing").status_code == 404
    assert client.get(
        "/v1/documents/doc_missing/versions/ver_missing/html"
    ).status_code == 404
    assert client.post(
        "/v1/documents/doc_missing/versions",
        json={"html": HTML_V1, "base_version_id": "ver_missing"},
    ).status_code == 404
