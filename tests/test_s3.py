"""S3 version storage tests (InMemoryBlobStore, no real S3 needed).

With a blob store attached, published HTML goes to the blob
store and the version row holds only s3_key; the API serves the
bytes back after verifying them against the stored SHA-256.
Without a blob store, HTML stays inline in the row, as before.
"""

import pytest
from fastapi.testclient import TestClient

from app.blob_store import InMemoryBlobStore, version_blob_key
from app.main import app, set_store, store
from app.s3_backfill import backfill_versions
from app.store import Store

HTML_V1 = "<!doctype html><html><body><h1>S3 v1</h1></body></html>"
HTML_V2 = "<!doctype html><html><body><h1>S3 v2</h1></body></html>"
HTML_V3 = "<!doctype html><html><body><h1>S3 v3</h1></body></html>"


@pytest.fixture()
def s3_client(tmp_path):
    blob = InMemoryBlobStore()
    s = Store(f"sqlite:///{tmp_path}/s3.db", blob_store=blob)
    set_store(s)
    with TestClient(app) as c:
        yield c, s, blob
    set_store(None)


def _create(client):
    r = client.post(
        "/v1/documents",
        json={"title": "S3 report", "html": HTML_V1, "change_summary": "v1"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_s3_create_stores_key_not_inline_html(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id, v1 = created["id"], created["version"]

    r = client.get(f"/v1/documents/{doc_id}/versions/{v1['id']}/html")
    assert r.status_code == 200 and r.text == HTML_V1

    version = s.get_version(doc_id, v1["id"])
    assert version.s3_key == version_blob_key(doc_id, v1["id"])
    assert version.html == ""
    assert blob.get(version.s3_key).decode("utf-8") == HTML_V1
    assert client.get(f"/v1/documents/{doc_id}/versions/{v1['id']}").json()["version"]["s3_key"] == version.s3_key


def test_s3_publish_and_draft_publish(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id, v1 = created["id"], created["version"]

    r = client.post(
        f"/v1/documents/{doc_id}/versions",
        json={"html": HTML_V2, "base_version_id": v1["id"], "change_summary": "v2"},
    )
    assert r.status_code == 201, r.text
    v2 = r.json()["version"]
    assert v2["number"] == 2
    assert client.get(f"/v1/documents/{doc_id}/versions/{v2['id']}/html").text == HTML_V2

    assert client.put(f"/v1/documents/{doc_id}/draft", json={"html": HTML_V3}).status_code == 200
    r = client.post(f"/v1/documents/{doc_id}/draft/publish", json={})
    assert r.status_code == 201, r.text
    v3 = r.json()["version"]
    assert v3["number"] == 3
    assert client.get(f"/v1/documents/{doc_id}/versions/{v3['id']}/html").text == HTML_V3
    assert s.get_version(doc_id, v3["id"]).s3_key == version_blob_key(doc_id, v3["id"])
    # Drafts themselves never went to the blob store.
    assert client.get(f"/v1/documents/{doc_id}/draft").status_code == 404


def test_s3_missing_blob_returns_500(s3_client):
    client, s, blob = s3_client
    created = _create(client)
    doc_id, v1 = created["id"], created["version"]
    assert blob.delete(version_blob_key(doc_id, v1["id"])) is True

    r = client.get(f"/v1/documents/{doc_id}/versions/{v1['id']}/html")
    assert r.status_code == 500
    assert r.json()["detail"] == "version_blob_not_found"


def test_s3_backfill_moves_inline_versions_and_rerun_skips(tmp_path):
    # Start inline: a store with no blob store at all.
    s = Store(f"sqlite:///{tmp_path}/backfill.db")
    set_store(s)
    try:
        with TestClient(app) as client:
            created = _create(client)
            doc_id, v1 = created["id"], created["version"]
            r = client.post(
                f"/v1/documents/{doc_id}/versions",
                json={"html": HTML_V2, "base_version_id": v1["id"], "change_summary": "v2"},
            )
            assert r.status_code == 201, r.text
            v2 = r.json()["version"]
            assert s.get_version(doc_id, v1["id"]).html == HTML_V1

            blob = InMemoryBlobStore()
            counts = backfill_versions(s, blob)
            assert counts == {"migrated": 2, "skipped": 0, "failed": 0}
            for vid, html in ((v1["id"], HTML_V1), (v2["id"], HTML_V2)):
                version = s.get_version(doc_id, vid)
                assert version.s3_key == version_blob_key(doc_id, vid)
                assert version.html == ""

            # A second run is a no-op for migrated versions.
            assert backfill_versions(s, blob) == {"migrated": 0, "skipped": 2, "failed": 0}

            # Attach the blob store and the HTML endpoint serves
            # from the blob, byte-identical.
            s.blob_store = blob
            assert store().blob_store is blob
            assert client.get(f"/v1/documents/{doc_id}/versions/{v1['id']}/html").text == HTML_V1
            assert client.get(f"/v1/documents/{doc_id}/versions/{v2['id']}/html").text == HTML_V2
    finally:
        set_store(None)
