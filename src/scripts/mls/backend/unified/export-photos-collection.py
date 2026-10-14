#!/usr/bin/env python3
"""
Export db.photos to a gzipped JSONL archive (one-shot, pre-retirement).

Why this exists
---------------
`db.photos` is the legacy primary-photo cache: one document PER PHOTO
(1,056,867 docs for ~86k listings), written by a PC-side legacy job and by
the now-suspended `cache-photos.py`. It has been superseded by `media[]`
stored inline on each listing doc, which `fetch-photos.py` fills from
Spark's per-listing photos sub-resource.

The collection is being dropped. This VPS has no database backups, so the
drop is preceded by a full export to a file outside the repo. Restoring is
a plain `mongoimport`-shaped replay of the JSONL.

The export is deliberately dumb: no transformation, no field selection, no
filtering. Whatever is in the collection lands in the file, `_id` included,
so a restore reproduces the collection exactly.

Usage:
    python3 src/scripts/mls/backend/unified/export-photos-collection.py \
        --out /root/backups/photos-YYYYMMDD.jsonl.gz
"""

import os
import sys
import gzip
import json
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(dotenv_path=Path(__file__).resolve().parents[5] / ".env.local")
MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise SystemExit("MONGODB_URI missing from .env.local")


def main() -> int:
    ap = argparse.ArgumentParser(description="Export db.photos to gzipped JSONL.")
    ap.add_argument("--out", required=True, help="Output path (.jsonl.gz).")
    ap.add_argument("--collection", default="photos")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 10-minute socket timeout: a full-collection scan of 1M docs under the
    # default 20s timeout is what killed the closed-sync seed step earlier.
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000,
                         socketTimeoutMS=600_000)
    col = client["admin"][args.collection]

    expected = col.count_documents({})
    print(f"[INFO] {args.collection}: {expected:,} documents")
    print(f"[INFO] → {out}")

    start = time.time()
    written = 0
    # json_util keeps BSON types ($oid, $date) round-trippable.
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=6) as fh:
        for doc in col.find({}, no_cursor_timeout=True).batch_size(2000):
            fh.write(json_util.dumps(doc))
            fh.write("\n")
            written += 1
            if written % 100_000 == 0:
                rate = written / (time.time() - start)
                print(f"  {written:>9,} / {expected:,}  ({rate:,.0f} docs/s)")

    elapsed = time.time() - start
    size = out.stat().st_size

    sha = hashlib.sha256()
    with open(out, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)

    print(f"\n[DONE] wrote {written:,} docs in {elapsed/60:.1f} min")
    print(f"  size:   {size/1024/1024:.1f} MB")
    print(f"  sha256: {sha.hexdigest()}")

    manifest = {
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "collection": args.collection,
        "expected_documents": expected,
        "written_documents": written,
        "complete": written == expected,
        "file": str(out),
        "bytes": size,
        "sha256": sha.hexdigest(),
        "format": "gzipped JSONL, bson json_util extended JSON, one doc per line",
        "restore": f"zcat {out} | mongoimport --uri <uri> --collection {args.collection}",
    }
    mpath = out.with_suffix(out.suffix + ".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2))
    print(f"  manifest: {mpath}")

    if written != expected:
        print(f"[ERROR] count mismatch: expected {expected:,}, wrote {written:,}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
