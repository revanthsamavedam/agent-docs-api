"""API tests: publish, view, version history, shared state,
registry, usage beacon, and audit chain (new-schema)."""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.blob_store import InMemoryBlobStore, version_blob_key
from app.main import app, set_store
from app.main import store as current_store
from app.store import Store, _audit_hash

HTML_V1 = "<!doctype html><html><body><h1>Report v1</h1></body></html>"
HTML_V2 = "<!doctype html><html><body><h1>Report v2</h1></body></html>"
HTML_V3 = "<!doctype html><html><body><h1>Report v3</h1></body></html>"


@pytest.fixture()
def client(tmp_path):
    # Version content is S3-only: tests attach the in-memory blob.
    set_store(Store(f"sqlite:///{tmp_path}/test.db", blob_store=InMemoryBlobStore()))
    with TestClient(app) as c:
        yield c
    set_store(None)


@pytest.fixture()
def plain_client(tmp_path):
    # No blob store: publishing must fail, by design.
    set_store(Store(f"sqlite:///{tmp_path}/plain.db"))
    with TestClient(app) as c:
        yield c
    set_store(None)


def _create(client):
    r = client.post("/v1/documents", json={"title": "Q3 report", "html": HTML_V1})
    assert r.status_code == 201, r.text
    return r.json()


def _publish(client, doc_id, html):
    r = client.post(f"/v1/documents/{doc_id}/versions", json={"html": html})
    assert r.status_code == 201, r.text
    return r.json()["version"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_get(client):
    created = _create(client)
    doc_id = created["id"]
    assert created["version"]["number"] == 1
    assert len(created["version"]["sha256"]) == 64
    assert created["version"]["s3_key"] == version_blob_key(doc_id, 1)

    docs = client.get("/v1/documents").json()["documents"]
    assert [d["id"] for d in docs] == [doc_id]
    assert docs[0]["latest_version"] == 1

    got = client.get(f"/v1/documents/{doc_id}").json()
    assert got["title"] == "Q3 report"
    assert got["latest_version"]["number"] == 1


def test_publish_appends_versions_and_old_version_is_immutable(client):
    created = _create(client)
    doc_id = created["id"]

    # No base version, no 409: publishes just append.
    v2 = _publish(client, doc_id, HTML_V2)
    assert v2["number"] == 2
    v3 = _publish(client, doc_id, HTML_V3)
    assert v3["number"] == 3

    # Versions are addressed by NUMBER, and old HTML is untouched.
    assert client.get(f"/v1/documents/{doc_id}/versions/1/html").text == HTML_V1
    assert client.get(f"/v1/documents/{doc_id}/versions/2/html").text == HTML_V2
    assert client.get(f"/v1/documents/{doc_id}/versions/3/html").text == HTML_V3
    assert client.get(f"/v1/documents/{doc_id}/versions/1/html").headers["content-type"].startswith("text/html")

    versions = client.get(f"/v1/documents/{doc_id}/versions").json()["versions"]
    assert [v["number"] for v in versions] == [1, 2, 3]
    assert client.get(f"/v1/documents/{doc_id}/versions/2").json()["version"]["s3_key"] == version_blob_key(doc_id, 2)

    # An explicit unknown version 404s — never falls back to latest.
    assert client.get(f"/v1/documents/{doc_id}/versions/99").status_code == 404
    assert client.get(f"/v1/documents/{doc_id}/versions/99/html").status_code == 404


def test_publish_without_blob_store_fails(plain_client):
    r = plain_client.post("/v1/documents", json={"title": "No blob", "html": HTML_V1})
    assert r.status_code == 500
    assert r.json()["detail"] == "blob_store_not_configured"
    assert plain_client.get("/v1/documents").json()["documents"] == []


def test_shared_state_is_document_scoped_not_version_scoped(client):
    created = _create(client)
    doc_id = created["id"]

    r = client.put(f"/v1/documents/{doc_id}/state/checklist.evidence_verified", json={"value": True})
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/documents/{doc_id}/state", json={"patch": {"approval.status": "approved", "gone": None}})
    assert r.status_code == 200, r.text
    state = client.get(f"/v1/documents/{doc_id}/state").json()["state"]
    assert state == {"approval.status": "approved", "checklist.evidence_verified": True}

    # Publish v2: state survives, and changing state creates no version.
    _publish(client, doc_id, HTML_V2)
    assert client.get(f"/v1/documents/{doc_id}/state").json()["state"] == state
    assert len(client.get(f"/v1/documents/{doc_id}/versions").json()["versions"]) == 2

    # Null PUT deletes; bad key is rejected; missing doc is 404.
    assert client.put(f"/v1/documents/{doc_id}/state/approval.status", json={"value": None}).json()["deleted"] is True
    assert client.get(f"/v1/documents/{doc_id}/state/approval.status").status_code == 404
    assert client.put(f"/v1/documents/{doc_id}/state/bad key!", json={"value": 1}).status_code == 400
    assert client.get("/v1/documents/doc_missing/state").status_code == 404


def test_api_registry_register_list_update_delete(client):
    r = client.post("/v1/apis", json={"app_name": "reports", "base_url": "https://internal-a.example/api/", "token_url": "https://internal-a.example/token"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["app_name"] == "reports"
    # No token -> registered by user_id "anonymous".
    assert r.json()["api"]["user_id"] == "anonymous"
    # A bearer token stamps its value as user_id; list filters by it.
    r = client.post("/v1/apis", json={"app_name": "mine", "base_url": "https://internal-a.example/api/", "token_url": "https://internal-a.example/token"}, headers={"Authorization": "Bearer user-123"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["user_id"] == "user-123"
    assert [a["app_name"] for a in client.get("/v1/apis", params={"user_id": "user-123"}).json()["apis"]] == ["mine"]
    assert [a["app_name"] for a in client.get("/v1/apis", params={"user_id": "anonymous"}).json()["apis"]] == ["reports"]
    client.delete("/v1/apis/mine")
    # Duplicate app_name -> 409; bad name / non-https URL -> 400.
    assert client.post("/v1/apis", json={"app_name": "reports", "base_url": "https://x.example/", "token_url": "https://x.example/token"}).status_code == 409
    assert client.post("/v1/apis", json={"app_name": "Bad Name", "base_url": "https://x.example/", "token_url": "https://x.example/token"}).status_code == 400
    assert client.post("/v1/apis", json={"app_name": "x", "base_url": "http://x.example/", "token_url": "https://x.example/token"}).status_code == 400
    # Slugs are lowercase, so an uppercase name is rejected — but
    # lookups are case-insensitive (CITEXT in Postgres, NOCASE here).
    assert client.post("/v1/apis", json={"app_name": "REPORTS", "base_url": "https://x.example/", "token_url": "https://x.example/token"}).status_code == 400
    assert client.get("/v1/apis/REPORTS").json()["api"]["app_name"] == "reports"

    apis = client.get("/v1/apis").json()["apis"]
    assert [a["app_name"] for a in apis] == ["reports"]
    assert client.get("/v1/apis/reports").json()["api"]["token_url"] == "https://internal-a.example/token"

    # Rotating a token URL does not require republishing documents:
    # the registry is resolved at view time.
    r = client.put("/v1/apis/reports", json={"token_url": "https://internal-a.example/new-token"})
    assert r.status_code == 200 and r.json()["api"]["token_url"] == "https://internal-a.example/new-token"
    # Explicit null clears a nullable column; absent keeps it.
    r = client.put("/v1/apis/reports", json={"token_url": None})
    assert r.status_code == 200 and r.json()["api"]["token_url"] is None
    assert client.put("/v1/apis/missing", json={"token_url": "https://x.example/token"}).status_code == 404

    assert client.delete("/v1/apis/reports").json() == {"app_name": "reports", "deleted": True}
    assert client.get("/v1/apis/reports").status_code == 404


def test_api_registry_nullable_token_url_for_apikey_schemes(client):
    # An apiKey scheme has no token endpoint: register without one.
    r = client.post("/v1/apis", json={"app_name": "keyed", "base_url": "https://keyed.example/api/"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["token_url"] is None


def test_api_sync_from_stored_swagger_url(client, monkeypatch):
    # Register refdp with its swagger doc stored in the registry.
    r = client.post("/v1/apis", json={"app_name": "refdp", "base_url": "https://old.example/api/", "token_url": "https://old.example/token", "swagger_url": "https://refdp.example/swagger.json"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["swagger_url"] == "https://refdp.example/swagger.json"

    # Sync with NO swagger_url in the request: the backend fetches
    # the latest doc from the stored URL.
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
    assert data["api"]["base_url"] == "https://new.example/api/"
    assert data["api"]["token_url"] == "https://new.example/token"
    assert data["api"]["swagger_url"] == "https://refdp.example/swagger.json"

    # Second sync: endpoints unchanged, one removed is reported, not deleted.
    def fake_fetch2(url):
        return {"servers": [{"url": "https://new.example/api/"}], "paths": {"/reports": {}, "/new": {}}}
    monkeypatch.setattr(main_mod, "fetch_swagger_doc", fake_fetch2)
    r = client.post("/v1/apis/refdp/sync", json={})
    assert r.json()["added_endpoints"] == ["/new"]
    assert r.json()["removed_endpoints"] == ["/risks"]
    assert r.json()["unchanged_endpoints"] == ["/reports"]

    # Every sync wrote one api_sync_log row.
    conn = sqlite3.connect(current_store().path)
    rows = conn.execute("SELECT added, removed FROM api_sync_log ORDER BY id").fetchall()
    conn.close()
    assert rows[0][0] == 2 and rows[0][1] == "[]"
    assert rows[1][0] == 1 and rows[1][1] == '["/risks"]'


def test_api_sync_without_any_token_url_keeps_null(client, monkeypatch):
    # An apiKey app's spec has no oauth2 tokenUrl and the entry has
    # none either: sync succeeds and token_url stays null (no 400).
    client.post("/v1/apis", json={"app_name": "keyed", "base_url": "https://keyed.example/api/", "swagger_url": "https://keyed.example/swagger.json"})
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod, "fetch_swagger_doc",
        lambda url: {"servers": [{"url": "https://keyed.example/api/"}], "paths": {"/things": {}}},
    )
    r = client.post("/v1/apis/keyed/sync", json={})
    assert r.status_code == 200, r.text
    assert r.json()["api"]["token_url"] is None
    assert r.json()["added_endpoints"] == ["/things"]


def test_usage_beacon_strips_query_and_stamps_server_actor(client):
    created = _create(client)
    doc_id = created["id"]
    r = client.post(
        "/v1/usage",
        json={"document_id": doc_id, "version_no": 1, "app_name": "reports",
              "method": "GET", "path": "/reports/4821?ssn=123-45-6789",
              "status_code": 200, "duration_ms": 142},
        headers={"Authorization": "Bearer viewer-7"},
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"logged": True}

    conn = sqlite3.connect(current_store().path)
    row = conn.execute("SELECT * FROM api_call_log").fetchone()
    conn.close()
    assert row is not None
    # Query string never reached the log; actor came from the token.
    assert row[5] == "/reports/4821"
    assert row[9] == "viewer-7"
    assert row[2] == 1 and row[4] == "GET" and row[8] == 142

    # Bad method -> 400; no auth -> anonymous, still logged.
    r = client.post("/v1/usage", json={"document_id": doc_id, "app_name": "reports", "method": "PATCH", "path": "/x", "duration_ms": 1})
    assert r.status_code == 400
    r = client.post("/v1/usage", json={"document_id": doc_id, "app_name": "reports", "method": "GET", "path": "/x", "error": "unregistered_app", "duration_ms": 3})
    assert r.status_code == 201


def test_audit_chain_verifies(client):
    created = _create(client)
    doc_id = created["id"]
    _publish(client, doc_id, HTML_V2)
    client.post("/v1/apis", json={"app_name": "reports", "base_url": "https://internal-a.example/api/"})

    entries = current_store().list_audit()
    assert [e["action"] for e in entries] == ["created", "published", "registered"]
    # Chain replay: each hash commits to the previous hash + fields.
    prev = None
    for e in entries:
        assert e["prev_hash"] == prev
        assert e["entry_hash"] == _audit_hash(
            prev, e["entity_type"], e["entity_id"], e["action"], e["actor"], e["details"]
        )
        prev = e["entry_hash"]


def test_publish_validation_rejects_secrets_direct_fetch_external_and_react(client):
    bad_cases = {
        "secret_detected": "<html><body><script>const k = 'sk-live-1234567890abcdef'; const api_key = \"abc12345\";</script></body></html>",
        "direct_fetch": "<html><body><script>fetch('https://internal.example/api/reports').then(r => r.json());</script></body></html>",
        "external_resource": "<html><head><script src=\"https://cdn.example/lib.js\"></script></head><body>hi</body></html>",
        "token_url_in_html": "<html><body><script>const u = 'https://internal.example/oauth/token';</script></body></html>",
        "react_import": "<html><body><script type=\"module\">import React from 'react';</script></body></html>",
    }
    for code, html in bad_cases.items():
        r = client.post("/v1/documents", json={"title": "Bad", "html": html})
        assert r.status_code == 400, (code, r.text)
        detail = r.json()["detail"]
        assert detail["error"] == "html_validation_failed", detail
        assert code in [v["code"] for v in detail["violations"]], detail
        # The error must not echo the secret back.
        assert "sk-live" not in r.text and "abc12345" not in r.text
    # Nothing was stored by any rejected publish.
    assert client.get("/v1/documents").json()["documents"] == []


def test_publish_validation_allows_sample_pages_and_blocks_bad_new_version(client):
    from pathlib import Path

    sample = Path(__file__).parent.parent.parent / "agent-docs-sample-site"
    for name in ("home.html", "reports.html", "risks.html", "beautiful.html"):
        html = (sample / name).read_text(encoding="utf-8")
        r = client.post("/v1/documents", json={"title": name, "html": html})
        assert r.status_code == 201, (name, r.text)
    # A bad new version is rejected and leaves v1 untouched.
    doc_id = client.get("/v1/documents").json()["documents"][0]["id"]
    r = client.post(
        f"/v1/documents/{doc_id}/versions",
        json={"html": "<html><body><script>fetch('https://x.example/');</script></body></html>"},
    )
    assert r.status_code == 400
    assert len(client.get(f"/v1/documents/{doc_id}/versions").json()["versions"]) == 1


def test_no_document_size_cap(client):
    # No 10 MiB cap: an 11 MiB document must be accepted, not 413.
    html = "<html>" + "x" * (11 * 1024 * 1024) + "</html>"
    r = client.post("/v1/documents", json={"title": "Big", "html": html})
    assert r.status_code == 201, r.status_code


def test_not_found(client):
    assert client.get("/v1/documents/doc_missing").status_code == 404
    assert client.get("/v1/documents/doc_missing/versions/1/html").status_code == 404
    assert client.post(
        "/v1/documents/doc_missing/versions",
        json={"html": HTML_V1},
    ).status_code == 404
