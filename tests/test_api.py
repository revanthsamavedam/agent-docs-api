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


def test_api_registry_register_list_update_delete(client):
    r = client.post("/v1/apis", json={"name": "reports", "api_url": "https://internal-a.example/api/", "token_url": "https://internal-a.example/token", "scope": "reports.read"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["name"] == "reports"
    # No token -> registered by user_id "anonymous".
    assert r.json()["api"]["user_id"] == "anonymous"
    # A bearer token stamps its value as user_id; list filters by it.
    r = client.post("/v1/apis", json={"name": "mine", "api_url": "https://internal-a.example/api/", "token_url": "https://internal-a.example/token"}, headers={"Authorization": "Bearer user-123"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["user_id"] == "user-123"
    assert [a["name"] for a in client.get("/v1/apis", params={"user_id": "user-123"}).json()["apis"]] == ["mine"]
    assert [a["name"] for a in client.get("/v1/apis", params={"user_id": "anonymous"}).json()["apis"]] == ["reports"]
    client.delete("/v1/apis/mine")
    # Duplicate name -> 409; bad name / non-https URL -> 400.
    assert client.post("/v1/apis", json={"name": "reports", "api_url": "https://x.example/", "token_url": "https://x.example/token"}).status_code == 409
    assert client.post("/v1/apis", json={"name": "Bad Name", "api_url": "https://x.example/", "token_url": "https://x.example/token"}).status_code == 400
    assert client.post("/v1/apis", json={"name": "x", "api_url": "http://x.example/", "token_url": "https://x.example/token"}).status_code == 400

    apis = client.get("/v1/apis").json()["apis"]
    assert [a["name"] for a in apis] == ["reports"]
    assert client.get("/v1/apis/reports").json()["api"]["token_url"] == "https://internal-a.example/token"

    # Rotating a token URL does not require republishing documents:
    # the registry is resolved at view time.
    r = client.put("/v1/apis/reports", json={"token_url": "https://internal-a.example/new-token"})
    assert r.status_code == 200 and r.json()["api"]["token_url"] == "https://internal-a.example/new-token"
    assert client.put("/v1/apis/missing", json={"token_url": "https://x.example/token"}).status_code == 404

    assert client.delete("/v1/apis/reports").json()["deleted"] is True
    assert client.get("/v1/apis/reports").status_code == 404


def test_api_sync_from_stored_swagger_url(client, monkeypatch):
    # Register refdp with its swagger doc stored in its app config.
    r = client.post("/v1/apis", json={"name": "refdp", "api_url": "https://old.example/api/", "token_url": "https://old.example/token", "swagger_url": "https://refdp.example/swagger.json"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["app"] == "refdp"
    assert r.json()["api"]["swagger_url"] == "https://refdp.example/swagger.json"

    # Sync with NO swagger_url in the request: CLI just runs `apis sync --app refdp`
    # and the backend fetches the latest doc from the stored URL.
    import app.main as main_mod

    def fake_fetch(url):
        assert url == "https://refdp.example/swagger.json"
        return {
            "servers": [{"url": "https://new.example/api/"}],
            "paths": {"/reports": {}, "/risks": {}},
            "components": {"securitySchemes": {"oauth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "https://new.example/token"}}}}},
        }

    monkeypatch.setattr(main_mod, "fetch_swagger_doc", fake_fetch)
    r = client.post("/v1/apis/refdp/sync", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["base_url"] == "https://new.example/api/"
    assert data["token_url_changed"] is True
    assert data["added_endpoints"] == ["/reports", "/risks"]
    assert data["api"]["api_url"] == "https://new.example/api/"
    assert data["api"]["token_url"] == "https://new.example/token"
    assert data["api"]["swagger_url"] == "https://refdp.example/swagger.json"

    # Second sync: endpoints unchanged, one removed is reported, not deleted from docs.
    def fake_fetch2(url):
        return {"servers": [{"url": "https://new.example/api/"}], "paths": {"/reports": {}, "/new": {}}}
    monkeypatch.setattr(main_mod, "fetch_swagger_doc", fake_fetch2)
    r = client.post("/v1/apis/refdp/sync", json={})
    assert r.json()["added_endpoints"] == ["/new"]
    assert r.json()["removed_endpoints"] == ["/risks"]
    assert r.json()["unchanged_endpoints"] == ["/reports"]


def test_api_registry_user_id_migrated_from_old_actor(tmp_path):
    # A DB created before user_id existed: old row has actor only.
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE apis (name TEXT PRIMARY KEY, api_url TEXT NOT NULL,"
        " token_url TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',"
        " actor TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO apis (name, api_url, token_url, actor) VALUES (?, ?, ?, ?)",
        ("legacy", "https://old.example/api/", "https://old.example/token", "old-user"),
    )
    conn.commit()
    conn.close()
    store = Store(f"sqlite:///{db}")
    assert store.get_api("legacy")["user_id"] == "old-user"


def test_no_document_size_cap(client):
    # No 10 MiB cap: an 11 MiB document must be accepted, not 413.
    html = "<html>" + "x" * (11 * 1024 * 1024) + "</html>"
    r = client.post("/v1/documents", json={"title": "Big", "html": html, "change_summary": "big"})
    assert r.status_code == 201, r.status_code


def test_not_found(client):
    assert client.get("/v1/documents/doc_missing").status_code == 404
    assert client.get(
        "/v1/documents/doc_missing/versions/ver_missing/html"
    ).status_code == 404
    assert client.post(
        "/v1/documents/doc_missing/versions",
        json={"html": HTML_V1, "base_version_id": "ver_missing"},
    ).status_code == 404
