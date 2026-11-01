"""Backfill inline version HTML into S3, one version at a time.

Order per version, and why: upload first, verify the hash of what
came back, and only then swap the database row to the pointer and
clear the inline HTML. A failed run therefore never leaves a row
pointing at a missing object, and re-running it is a no-op for
versions already migrated (they are counted as skipped).

Usage (script wrapper in scripts/backfill_versions_to_s3.py):
    DATABASE_URL=postgresql://... AGENT_DOCS_S3_BUCKET=<bucket> \
        python scripts/backfill_versions_to_s3.py [--dry-run]

Drafts are not touched: they stay in the database by design.
Orphan objects (in S3, no database row) are not deleted here —
list them from the bucket and check before deleting anything.
"""

from __future__ import annotations

import hashlib

from .blob_store import version_blob_key


def backfill_versions(store, blob, dry_run: bool = False) -> dict:
    counts = {"migrated": 0, "skipped": 0, "failed": 0}
    for doc in store.list_documents():
        for version in store.list_versions(doc["id"]):
            if getattr(version, "s3_key", ""):
                counts["skipped"] += 1
                continue
            if not version.html:
                counts["failed"] += 1
                continue
            key = version_blob_key(doc["id"], version.id)
            if dry_run:
                counts["migrated"] += 1
                continue
            try:
                blob.put(key, version.html.encode("utf-8"))
                back = blob.get(key)
                if back is None or hashlib.sha256(back).hexdigest() != version.sha256:
                    counts["failed"] += 1
                    continue
                if store.migrate_version_to_s3(doc["id"], version.id, key):
                    counts["migrated"] += 1
                else:
                    counts["skipped"] += 1
            except Exception:
                counts["failed"] += 1
    return counts
