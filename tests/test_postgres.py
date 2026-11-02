"""Postgres store tests — live only.

Runs only when AGENTDOCS_TEST_POSTGRES_URL is set to a throwaway
database (the fixture drops all agent-docs tables in it first).
Skipped otherwise, so the normal suite never needs a Postgres
server.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient

from app.blob_store import InMemoryBlobStore, version_blob_key
from app.main import app, set_store
from app.postgres_store import PostgresStore
from app.store import get_store

URL = os.environ.get("AGENTDOCS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="AGENTDOCS_TEST_POSTGRES_URL not set")

HTML_V1 = "<!doctype html><html><body><h1>PG v1</h1></body></html>"
HTML_V2 = "<!doctype html><html><body><h1>PG v2</h1></body></html>"


def _drop_tables():
    with psycopg.connect(URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS api_sync_log, api_call_log, "
                "audit_events, api_registry, document_state, "
                "document_versions, documents CASCADE"
            )


@pytest.fixture()
def client():
    _drop_tables()
    # Version content is S3-only: tests attach the in-memory blob.
    store = PostgresStore(URL, blob_store=InMemoryBlobStore())
    set_store(store)
    with TestClient(app) as c:
        yield c
    set_store(None)


def test_get_store_routes_postgresql_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    assert type(get_store()).__name__ == "PostgresStore"


def test_postgres_full_flow(client):
    r = client.post("/v1/documents", json={"title": "PG report", "html": HTML_V1})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    assert r.json()["version"]["number"] == 1

    # Publish appends; versions are addressed by number.
    r = client.post(f"/v1/documents/{doc_id}/versions", json={"html": HTML_V2})
    assert r.status_code == 201 and r.json()["version"]["number"] == 2
    assert client.get(f"/v1/documents/{doc_id}/versions/1/html").text == HTML_V1
    assert client.get(f"/v1/documents/{doc_id}/versions/2/html").text == HTML_V2
    assert client.get(f"/v1/documents/{doc_id}/versions/99").status_code == 404

    # Shared state.
    assert client.put(f"/v1/documents/{doc_id}/state/checklist.done", json={"value": True}).status_code == 200
    assert client.get(f"/v1/documents/{doc_id}/state").json()["state"] == {"checklist.done": True}

    # Registry, stamped by user_id, filtered by it; nullable token.
    r = client.post("/v1/apis", json={"app_name": "pgapp", "base_url": "https://internal.example/api/", "token_url": "https://internal.example/token"}, headers={"Authorization": "Bearer pg-user"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["user_id"] == "pg-user"
    assert [a["app_name"] for a in client.get("/v1/apis", params={"user_id": "pg-user"}).json()["apis"]] == ["pgapp"]
    assert client.put("/v1/apis/pgapp", json={"token_url": "https://internal.example/new-token"}).status_code == 200
    assert client.delete("/v1/apis/pgapp").json()["deleted"] is True

    # Usage beacon.
    r = client.post("/v1/usage", json={"document_id": doc_id, "version_no": 2, "app_name": "pgapp", "method": "GET", "path": "/things?secret=1", "status_code": 200, "duration_ms": 5})
    assert r.status_code == 201


def test_postgres_s3_version_storage():
    """Postgres + blob store: publish goes to the blob, the row
    holds only the key, and the HTML endpoint serves it back."""
    _drop_tables()
    blob = InMemoryBlobStore()
    store = PostgresStore(URL, blob_store=blob)
    set_store(store)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/documents", json={"title": "PG S3", "html": HTML_V1})
            assert r.status_code == 201, r.text
            doc_id = r.json()["id"]
            version = store.get_version(doc_id, 1)
            assert version.s3_key == version_blob_key(doc_id, 1)
            assert client.get(f"/v1/documents/{doc_id}/versions/1/html").text == HTML_V1
    finally:
        set_store(None)
