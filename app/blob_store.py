"""Blob storage for published version HTML (S3-backed, required).

Version content lives ONLY here: the database row holds the
object key (``document_versions.s3_key``) plus the SHA-256.
The API serves the bytes back after its own hash check — the
viewer never talks to S3, never sees a bucket URL, and never
receives a presigned URL (those expire, and a leaked one *is*
access).

Key scheme: ``docs/<doc_id>/v<version_no>.html`` — one key per
version, written once, never overwritten (a retried publish
after a version-number collision writes a new key; the orphan
from the failed attempt is harmless). S3's own versioning is
NOT the document history; the version number is.

Credentials: boto3's default chain (environment, IAM role, or
the service account in the service tier). No credential is ever
in code, in the database, or in a document. The bucket is
private; the API's service account is its only reader.

There is no inline-HTML fallback: publishing without a blob
store configured fails, by design.
"""

from __future__ import annotations

import os


def version_blob_key(doc_id: str, version_no: int) -> str:
    return f"docs/{doc_id}/v{version_no}.html"


class InMemoryBlobStore:
    """Test double: same put/get/delete shape as S3BlobStore."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None


class S3BlobStore:
    """Thin boto3 wrapper. One bucket, private, API-served only."""

    def __init__(self, bucket: str, endpoint_url: str | None = None, region_name: str | None = None):
        try:
            import boto3
        except ImportError as e:  # pragma: no cover - missing optional dep
            raise RuntimeError(
                "S3 version storage needs boto3: "
                "pip install agent-docs-api[s3]"
            ) from e
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
        )

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as e:
            # S3-compatible stores surface a missing key as a
            # ClientError with Code=NoSuchKey rather than the
            # typed exception above; treat that the same way.
            if getattr(e, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise
        return resp["Body"].read()

    def delete(self, key: str) -> bool:
        self._client.delete_object(Bucket=self.bucket, Key=key)
        return True


def blob_store_from_env():
    """S3BlobStore from the service environment, or None.

    AGENT_DOCS_S3_BUCKET (required to enable), AGENT_DOCS_S3_ENDPOINT_URL
    (optional, for S3-compatible storage), AGENT_DOCS_S3_REGION (optional).
    None means publishing fails: version content is S3-only.
    """
    bucket = os.environ.get("AGENT_DOCS_S3_BUCKET")
    if not bucket:
        return None
    return S3BlobStore(
        bucket,
        endpoint_url=os.environ.get("AGENT_DOCS_S3_ENDPOINT_URL"),
        region_name=os.environ.get("AGENT_DOCS_S3_REGION"),
    )
