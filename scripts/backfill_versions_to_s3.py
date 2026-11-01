#!/usr/bin/env python3
"""Backfill inline version HTML into S3 (see app/s3_backfill.py).

    DATABASE_URL=postgresql://<service-account>@<host>:5432/agentdocs \
    AGENT_DOCS_S3_BUCKET=<bucket> \
        python scripts/backfill_versions_to_s3.py [--dry-run]

--dry-run counts what would migrate without uploading anything.
Exit code is 1 if any version failed, so a deploy pipeline can
stop on a partial backfill instead of silently proceeding.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.blob_store import blob_store_from_env
from app.s3_backfill import backfill_versions
from app.store import get_store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args()
    blob = blob_store_from_env()
    if blob is None:
        print("AGENT_DOCS_S3_BUCKET is not set; nothing to backfill into.", file=sys.stderr)
        return 2
    counts = backfill_versions(get_store(), blob, dry_run=opts.dry_run)
    print(f"migrated={counts['migrated']} skipped={counts['skipped']} failed={counts['failed']}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
