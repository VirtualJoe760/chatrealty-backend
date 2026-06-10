#!/usr/bin/env python3
"""
Cache Primary Photos (unified_listings → db.photos)

Unified-collection successor to the legacy `cache_photos.py` that pulled from
`db.listings`. Populates the separate `db.photos` collection with one document
per listing holding just the primary photo's URI variants. The frontend's
listing-list / map / card views query `db.photos` directly (by `listingId`)
for O(1) primary-photo lookup — much faster than reading a full `media[]`
off every `unified_listings` doc.

This complements `fetch-photos.py`, which writes the *full* media array
inline on each listing. Run order in crontab:

    0  9 * * *  unified/cache-photos.py             (primary photos → db.photos)
   30  9 * * *  unified/fetch-photos.py --all --delta  (full media[] → unified_listings.media)

Resumable via a JSON skip index (`local-logs/photo-logs/skip_index.json`)
containing listingKeys that returned 403 / no-photos / no-photo-id — those
listings are never re-tried.

Usage:
    # Routine run
    python3 src/scripts/mls/backend/unified/cache-photos.py

    # Scoped test
    python3 src/scripts/mls/backend/unified/cache-photos.py --mls GPS --limit 50

    # Ignore skip-index (retry everything)
    python3 src/scripts/mls/backend/unified/cache-photos.py --force
"""

import os
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from typing import Any, Optional

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV & CONFIG
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")
BASE_URL = "https://replication.sparkapi.com/v1/listings"

if not ACCESS_TOKEN or not MONGO_URI:
    raise ValueError("❌ Missing SPARK_ACCESS_TOKEN or MONGODB_URI in .env.local")

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

MLS_NAMES = [
    "GPS", "CRMLS", "CLAW", "SOUTHLAND",
    "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK", "ITECH",
]

MAX_WORKERS = 2
REQUEST_SLEEP = 0.5
MAX_RETRIES = 3

LOG_DIR = Path(__file__).resolve().parents[5] / "local-logs" / "photo-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SKIP_INDEX_PATH = LOG_DIR / "skip_index.json"

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
RUN_LOG_PATH = LOG_DIR / f"run_{RUN_ID}.jsonl"

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=20000)
db = client.get_database()
photos_collection = db.photos
listings_collection = db.unified_listings
print("✅ Connected to MongoDB (unified_listings + photos)")

# ──────────────────────────────────────────────────────────────────────────────
# 🧾 SKIP-INDEX HELPERS
# ──────────────────────────────────────────────────────────────────────────────

_skip_ids: set[str] = set()


def load_skip_index() -> set[str]:
    if not SKIP_INDEX_PATH.exists():
        return set()
    try:
        with SKIP_INDEX_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "listingIds" in data:
            return {str(x) for x in data["listingIds"]}
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception as e:
        print(f"⚠️  Could not read skip index: {e}")
    return set()


def persist_skip_index(skip_ids: set[str]) -> None:
    tmp = SKIP_INDEX_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"listingIds": sorted(skip_ids)}, f, ensure_ascii=False, indent=2)
    tmp.replace(SKIP_INDEX_PATH)


def append_run_log(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def mark_skipped(listing_key: str, reason: str, extra: Optional[dict] = None) -> None:
    _skip_ids.add(listing_key)
    append_run_log({"event": "skipped", "listingKey": listing_key, "reason": reason, **(extra or {})})
    persist_skip_index(_skip_ids)


def mark_success(listing_key: str, photo_id: Optional[str]) -> None:
    append_run_log({"event": "cached", "listingKey": listing_key, "photoId": photo_id})


def mark_error(listing_key: str, msg: str) -> None:
    append_run_log({"event": "error", "listingKey": listing_key, "message": msg})

# ──────────────────────────────────────────────────────────────────────────────
# 🌐 SPARK
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_photos(listing_key: str):
    url = f"{BASE_URL}/{listing_key}/photos"
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                return res.json().get("D", {}).get("Results", [])
            if res.status_code == 403:
                return {"_403": True, "body": res.text[:500]}
            if res.status_code == 404:
                return []
            if res.status_code == 429 or "over rate" in res.text.lower():
                time.sleep(3 + attempt * 2)
                continue
            raise Exception(f"HTTP {res.status_code}: {res.text[:200]}")
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                raise Exception(f"max retries: {e}")
            time.sleep(2 ** attempt)
    raise Exception("max retries reached")

# ──────────────────────────────────────────────────────────────────────────────
# 📦 WORKER
# ──────────────────────────────────────────────────────────────────────────────


def _pick_primary(photos: list[dict]) -> Optional[dict]:
    if not photos:
        return None
    for p in photos:
        if (p.get("MediaCategory") or "").strip() == "Primary Photo":
            return p
    for p in photos:
        if p.get("Order") == 0 or p.get("Primary") is True:
            return p
    return photos[0]


def cache_photo_for_listing(listing: dict[str, Any]) -> str:
    listing_key = str(listing.get("listingKey") or "").strip()
    if not listing_key:
        return "⚠️  Skipped: missing listingKey"

    if listing_key in _skip_ids:
        return f"⏭️  Pre-skipped {listing_key}"

    if photos_collection.find_one({"listingId": listing_key}, {"_id": 1}):
        # Already cached — don't mark in skip-index (could be refreshed later).
        return f"⏩ Already cached {listing_key}"

    try:
        photos = fetch_listing_photos(listing_key)
    except Exception as e:
        mark_error(listing_key, str(e))
        return f"❌ Failed {listing_key}: {e}"

    if isinstance(photos, dict) and photos.get("_403"):
        mark_skipped(listing_key, "permission-denied", {"body": photos["body"]})
        return f"🚫 403 {listing_key} (skipped permanently)"

    if not photos:
        mark_skipped(listing_key, "no-photos")
        return f"⚠️  No photos {listing_key}"

    primary = _pick_primary(photos)
    if not primary:
        mark_skipped(listing_key, "no-primary")
        return f"⚠️  No primary photo {listing_key}"

    photo_id = primary.get("Id") or primary.get("MediaKey")
    if not photo_id:
        mark_skipped(listing_key, "no-photo-id")
        return f"⚠️  No photoId {listing_key}"

    doc = {
        # Dual key: listingId kept for backwards-compat with legacy collection;
        # listingKey explicit for the unified schema.
        "listingId": listing_key,
        "listingKey": listing_key,
        "mlsSource": listing.get("mlsSource"),
        "photoId": photo_id,
        "caption": primary.get("Caption"),
        "uriThumb": primary.get("UriThumb"),
        "uri300": primary.get("Uri300"),
        "uri640": primary.get("Uri640"),
        "uri800": primary.get("Uri800"),
        "uri1024": primary.get("Uri1024"),
        "uri1280": primary.get("Uri1280"),
        "uri1600": primary.get("Uri1600"),
        "uri2048": primary.get("Uri2048"),
        "uriLarge": primary.get("UriLarge"),
        "imageWidth": primary.get("ImageWidth"),
        "imageHeight": primary.get("ImageHeight"),
        "primary": primary.get("Primary", True),
        "cachedAt": datetime.now(timezone.utc).isoformat(),
    }

    photos_collection.update_one(
        {"listingId": listing_key},
        {"$set": doc},
        upsert=True,
    )
    mark_success(listing_key, photo_id)
    time.sleep(REQUEST_SLEEP)
    return f"✅ Cached {listing_key}"

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Cache primary photos for unified_listings into db.photos.")
    parser.add_argument("--mls", choices=MLS_NAMES, help="Restrict to a single MLS.")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore skip-index — retry every listing that isn't already in db.photos.")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Concurrent workers (default {MAX_WORKERS}).")
    args = parser.parse_args()

    global _skip_ids
    _skip_ids = set() if args.force else load_skip_index()
    print(f"🧾 Skip-index loaded: {len(_skip_ids):,} listingKeys")

    query: dict = {}
    if args.mls:
        query["mlsSource"] = args.mls

    cursor = listings_collection.find(query, {"listingKey": 1, "mlsSource": 1})
    if args.limit:
        cursor = cursor.limit(args.limit)

    listings = list(cursor)
    pre_count = len(listings)

    if not args.force:
        listings = [
            l for l in listings
            if l.get("listingKey") and str(l["listingKey"]) not in _skip_ids
        ]
    else:
        listings = [l for l in listings if l.get("listingKey")]

    print(f"🧹 Pre-filter: {pre_count:,} → {len(listings):,} after skip-index filter")
    if not listings:
        print("❌ Nothing to process")
        return

    processed = 0
    cached = 0
    skipped = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(cache_photo_for_listing, l) for l in listings]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                failed += 1
                print(f"❌ Worker crashed: {e}")
                continue
            processed += 1
            if "✅" in result:
                cached += 1
            elif "❌" in result:
                failed += 1
            else:
                skipped += 1
            if processed % 500 == 0:
                print(f"  [{processed:,}/{len(listings):,}] cached={cached:,} skipped={skipped:,} failed={failed:,}")
            print(result)

    append_run_log({
        "event": "run_complete",
        "processed": processed,
        "cached": cached,
        "skipped": skipped,
        "failed": failed,
        "skip_index_size": len(_skip_ids),
    })

    print("\n" + "=" * 60)
    print(f"🏁 Run complete")
    print(f"   processed: {processed:,}")
    print(f"   cached:    {cached:,}")
    print(f"   skipped:   {skipped:,}")
    print(f"   failed:    {failed:,}")
    print(f"   skip_index size: {len(_skip_ids):,}")
    print(f"   run log: {RUN_LOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Unhandled error in cache-photos.py: {e}")
        raise
