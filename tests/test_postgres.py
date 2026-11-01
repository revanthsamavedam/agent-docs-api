"""Postgres store tests — live only.

Runs only when AGENTDOCS_TEST_POSTGRES_URL is set to a throwaway
database (the fixture drops all agent-docs tables in it first).
Skipped otherwise, so the normal suite never needs a Postgres
server. In this sandbox the test database is a local Postgres 16
instance; at Wellington it is the pilot database.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient

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
                "DROP TABLE IF EXISTS versions, document_kv, apis, documents CASCADE"
            )


@pytest.fixture()
def client():
    _drop_tables()
    store = PostgresStore(URL)
    set_store(store)
    with TestClient(app) as c:
        yield c
    set_store(None)


def test_get_store_routes_postgresql_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    assert type(get_store()).__name__ == "PostgresStore"


def test_postgres_full_flow(client):
    r = client.post("/v1/documents", json={"title": "PG report", "html": HTML_V1, "change_summary": "v1"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    v1_id = r.json()["version"]["id"]

    # Publish v2, then a stale base 409s, exactly like SQLite.
    r = client.post(f"/v1/documents/{doc_id}/versions", json={"html": HTML_V2, "base_version_id": v1_id})
    assert r.status_code == 201 and r.json()["version"]["number"] == 2
    assert client.post(f"/v1/documents/{doc_id}/versions", json={"html": HTML_V2, "base_version_id": v1_id}).status_code == 409
    assert client.get(f"/v1/documents/{doc_id}/versions/{v1_id}/html").text == HTML_V1

    # Drafts: save, view, publish, cleared.
    assert client.put(f"/v1/documents/{doc_id}/draft", json={"html": HTML_V1}).status_code == 200
    assert client.get(f"/v1/documents/{doc_id}/draft").json()["html"] == HTML_V1
    r = client.post(f"/v1/documents/{doc_id}/draft/publish", json={})
    assert r.status_code == 201 and r.json()["version"]["number"] == 3
    assert client.get(f"/v1/documents/{doc_id}/draft").status_code == 404

    # Shared state.
    assert client.put(f"/v1/documents/{doc_id}/state/checklist.done", json={"value": True}).status_code == 200
    assert client.get(f"/v1/documents/{doc_id}/state").json()["state"] == {"checklist.done": True}

    # Registry, stamped by user_id, filtered by it.
    r = client.post("/v1/apis", json={"name": "pgapp", "api_url": "https://internal.example/api/", "token_url": "https://internal.example/token"}, headers={"Authorization": "Bearer pg-user"})
    assert r.status_code == 201, r.text
    assert r.json()["api"]["user_id"] == "pg-user"
    assert [a["name"] for a in client.get("/v1/apis", params={"user_id": "pg-user"}).json()["apis"]] == ["pgapp"]
    assert client.put("/v1/apis/pgapp", json={"token_url": "https://internal.example/new-token"}).status_code == 200
    assert client.delete("/v1/apis/pgapp").json()["deleted"] is True


def test_postgres_schema_migrates_old_database():
    _drop_tables()
    with psycopg.connect(URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "actor TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute(
                "CREATE TABLE apis (name TEXT PRIMARY KEY, api_url TEXT NOT NULL, "
                "token_url TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', "
                "actor TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute(
                "INSERT INTO apis (name, api_url, token_url, actor) VALUES "
                "('legacy', 'https://old.example/api/', 'https://old.example/token', 'legacy-user')"
            )
    store = PostgresStore(URL)
    assert store.get_api("legacy")["user_id"] == "legacy-user"
    # Draft columns were added by the same migration.
    assert store.set_draft("doc_missing", "<html></html>") is False
