"""S3 version storage tests (InMemoryBlobStore, no real S3 needed).

Version content is S3-only: published HTML always goes to the
blob store and the version row holds only s3_key. The API serves
the bytes back after verifying them against the stored SHA-256.
Publishing without a blob store fails, by design — there is no
inline-HTML fallback anymore.
"""

import pytest
from fastapi.testclient import TestClient

from app.blob_store import InMemoryBlobStore, version_blob_key
from app.main import app, set_store
from app.store import Store

HTML_V1 = "<!doctype html><html><body><h1>S3 v1</h1></body></html>"
HTML_V2 = "<!doctype html><html><body><h1>S3 v2</h1></body></html>"


@pytest.fixture()
def s3_client(tmp_path):
    blob = InMemoryBlobStore()
    s = Store(f"sqlite:///{tmp_path}/s3.db", blob_store=blob)
    set_store(s)
    with TestClient(app) as c:
        yield c, s, blob
    set_store(None)


def _create(client):
    r = client.post("/v1/documents", json={"title": "S3 report", "html": HTML_V1})
    assert r.status_code == 201, r.text
    return r.json()


def test_s3_create_stores_key_not_inline_html(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id, v1 = created["id"], created["version"]

    r = client.get(f"/v1/documents/{doc_id}/versions/1/html")
    assert r.status_code == 200 and r.text == HTML_V1

    version = s.get_version(doc_id, 1)
    assert version.s3_key == version_blob_key(doc_id, 1)
    assert version.s3_key == "docs/%s/v1.html" % doc_id
    assert blob.get(version.s3_key).decode("utf-8") == HTML_V1
    assert client.get(f"/v1/documents/{doc_id}/versions/1").json()["version"]["s3_key"] == version.s3_key


def test_s3_publish_writes_next_version_key(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id = created["id"]

    r = client.post(f"/v1/documents/{doc_id}/versions", json={"html": HTML_V2})
    assert r.status_code == 201, r.text
    v2 = r.json()["version"]
    assert v2["number"] == 2
    assert client.get(f"/v1/documents/{doc_id}/versions/2/html").text == HTML_V2
    assert s.get_version(doc_id, 2).s3_key == version_blob_key(doc_id, 2)


def test_s3_missing_blob_returns_500(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id = created["id"]
    assert blob.delete(version_blob_key(doc_id, 1)) is True

    r = client.get(f"/v1/documents/{doc_id}/versions/1/html")
    assert r.status_code == 500
    assert r.json()["detail"] == "version_blob_not_found"


def test_s3_hash_mismatch_returns_500(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id = created["id"]
    # Swap the object for different bytes: the stored hash no
    # longer matches, so the API must refuse to serve it.
    blob.put(version_blob_key(doc_id, 1), b"<html>tampered</html>")

    r = client.get(f"/v1/documents/{doc_id}/versions/1/html")
    assert r.status_code == 500
    assert r.json()["detail"] == "version_blob_hash_mismatch"


def test_s3_publish_without_blob_store_fails(tmp_path):
    s = Store(f"sqlite:///{tmp_path}/noblob.db")
    set_store(s)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/documents", json={"title": "No blob", "html": HTML_V1})
            assert r.status_code == 500
            assert r.json()["detail"] == "blob_store_not_configured"
    finally:
        set_store(None)
