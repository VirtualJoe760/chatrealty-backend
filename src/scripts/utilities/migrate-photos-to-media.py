#!/usr/bin/env python3
"""
Migrate legacy db.photos documents into unified_listings.media[] (fallback).

Why this exists
---------------
`db.photos` is being retired in favour of `media[]` stored inline on the
listing. Before the collection is dropped, anything it holds that is NOT
already represented in `media[]` has to be moved, or those listings lose
their only photo.

This is the FALLBACK half of that migration. The primary repair is
`fetch-photos.py --missing-media`, which pulls the complete photo set from
Spark's per-listing sub-resource — and should always be run first, because
db.photos is a *primary-photo cache*, not a full mirror: for the live
listings that were missing media[], it held 1–7 photos each where the feed
carries 9–67. Migrating from db.photos when the feed is reachable would
pin a listing to one thumbnail instead of its real gallery.

So the order is:
  1. fetch-photos.py --missing-media   (authoritative, full galleries)
  2. this script                       (whatever the feed would not serve)

A listing is only touched if media[] is still absent/empty after step 1.
Nothing is ever overwritten.

Provenance: every element written here carries mediaSource="db.photos" and
mediaMigratedAt, so a thin legacy gallery is always distinguishable from a
real feed fetch, and `photoCachedAt` is deliberately NOT stamped — these
listings should stay eligible for a future feed fetch.

Usage:
    # See what would move, write nothing
    python3 src/scripts/utilities/migrate-photos-to-media.py --dry-run

    # Do it
    python3 src/scripts/utilities/migrate-photos-to-media.py --apply
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env.local")
MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise SystemExit("MONGODB_URI missing from .env.local")

LIFECYCLE_COLLECTIONS = ("unified_listings", "unified_in_escrow", "unified_off_market")

# The URI variants db.photos carries. Copied straight across — both the
# legacy PC writer and cache-photos.py use these exact names, and so does
# the media[] element shape that fetch-photos.py writes.
URI_FIELDS = ("uriThumb", "uri300", "uri640", "uri800",
              "uri1024", "uri1280", "uri1600", "uri2048", "uriLarge")

LOG_DIR = Path(__file__).resolve().parents[3] / "local-logs" / "photo-fetch"


def to_media_element(photo: dict, migrated_at: str) -> dict:
    """Shape a db.photos document like a fetch-photos.py media[] element."""
    el = {
        "id": photo.get("photoId"),
        "name": photo.get("caption") or ("Primary Photo" if photo.get("primary") else ""),
        "primary": bool(photo.get("primary")),
        "mediaSource": "db.photos",
        "mediaMigratedAt": migrated_at,
    }
    for f in URI_FIELDS:
        if photo.get(f):
            el[f] = photo[f]
    return el


def photos_for(photos_col, listing_id, listing_key) -> list[dict]:
    """
    db.photos has TWO writers with two key conventions:
      - legacy PC writer: listingId = the MLS listing number ("219127765")
      - cache-photos.py:  listingId = listingKey, plus a listingKey field
    Try the listing's own listingId first, then its listingKey.
    """
    for query in ({"listingId": listing_id}, {"listingKey": listing_key},
                  {"listingId": listing_key}):
        if not query[next(iter(query))]:
            continue
        docs = list(photos_col.find(query))
        if docs:
            return docs
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate db.photos → unified media[] (fallback).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
    g.add_argument("--apply", action="store_true", help="Write the media[] arrays.")
    ap.add_argument("--collection", choices=LIFECYCLE_COLLECTIONS, action="append",
                    help="Restrict to one lifecycle collection (repeatable). Default: all three.")
    args = ap.parse_args()

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000, socketTimeoutMS=600_000)
    db = client["admin"]
    photos_col = db["photos"]
    targets = tuple(args.collection) if args.collection else LIFECYCLE_COLLECTIONS
    migrated_at = datetime.now(timezone.utc).isoformat()

    summary = {"mode": "apply" if args.apply else "dry-run",
               "timestamp_utc": migrated_at, "collections": {}}
    grand_listings = grand_photos = 0

    for name in targets:
        col = db[name]
        gap = list(col.find({"media.0": {"$exists": False}},
                            {"listingKey": 1, "listingId": 1, "mlsSource": 1}))
        ops, moved_photos, no_source = [], 0, 0

        for d in gap:
            photos = photos_for(photos_col, d.get("listingId"), d.get("listingKey"))
            if not photos:
                no_source += 1
                continue
            # primary first, then whatever order the cache held them in
            photos.sort(key=lambda p: (not p.get("primary"), str(p.get("photoId") or "")))
            media = [to_media_element(p, migrated_at) for p in photos]
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": {"media": media}}))
            moved_photos += len(media)

        print(f"{name}:")
        print(f"  listings with no media[]      {len(gap):,}")
        print(f"  → recoverable from db.photos  {len(ops):,}  ({moved_photos:,} photos)")
        print(f"  → no photos in either source  {no_source:,}")

        if args.apply and ops:
            res = col.bulk_write(ops, ordered=False)
            print(f"  WROTE modified={res.modified_count:,}")
            summary["collections"][name] = {"gap": len(gap), "migrated": res.modified_count,
                                            "photos": moved_photos, "no_source": no_source}
        else:
            summary["collections"][name] = {"gap": len(gap), "migrated": 0 if args.dry_run else len(ops),
                                            "photos": moved_photos, "no_source": no_source}

        grand_listings += len(ops)
        grand_photos += moved_photos

    print(f"\n[{'APPLIED' if args.apply else 'DRY-RUN'}] "
          f"{grand_listings:,} listings / {grand_photos:,} photos")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"migrate_photos_to_media_{ts}.json"
    out.write_text(json.dumps(summary, indent=2))
    print(f"  → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
