#!/usr/bin/env python3
"""
Fetch Photos (unified_listings)

Calls Spark's per-listing photos sub-resource and writes the result to the
`media` array on each listing doc in `unified_listings`. The Spark bulk
`_expand=Photos` on /v1/listings silently drops the expansion, so photos
must be fetched one listing at a time via:

    GET /v1/listings/{listingKey}/photos

Each photo object is camelCase-transformed before being stored so it matches
the IMedia Mongoose schema on the frontend.

Three run modes:

  - Default: fetch photos for every unified_listings doc that has no
    `photoCachedAt`. Resumable — re-runs skip docs already marked.
  - `--delta`: additionally re-fetch docs whose `modificationTimestamp`
    is newer than their `photoCachedAt`. This is the daily cron mode.
  - `--force`: ignore `photoCachedAt` entirely and re-fetch every matched
    doc.

Rate-limit tuning is deliberately conservative (1 worker, 0.5 s between
requests, exponential backoff on 429). The first backfill run with 3
workers rate-limited hard; 1-worker was stable at ~2 req/sec. The daily
`--delta` run is small (~500–2000 listings/day) so the slower pace is fine.

Usage:
    # Small sanity test
    python3 src/scripts/mls/backend/unified/fetch-photos.py --mls GPS --limit 10

    # Full backfill across all MLSs (several hours)
    python3 src/scripts/mls/backend/unified/fetch-photos.py --all

    # Daily delta — re-fetch only listings modified since their photoCachedAt
    python3 src/scripts/mls/backend/unified/fetch-photos.py --all --delta

    # Re-cache a specific MLS ignoring existing photoCachedAt
    python3 src/scripts/mls/backend/unified/fetch-photos.py --mls CRMLS --force
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

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

DEFAULT_SLEEP = 0.5       # seconds between requests per worker
MAX_RETRIES = 5           # exponential backoff on 429/5xx
REQUEST_TIMEOUT = 30      # seconds

LOG_DIR = Path(__file__).resolve().parents[5] / "local-logs" / "photo-fetch"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# 🔁 camelCase transformer (matches flatten.py conventions)
# ──────────────────────────────────────────────────────────────────────────────

_CAMEL_SPLIT_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _camel(key: str) -> str:
    # PascalCase → camelCase: "Uri800" → "uri800", "MediaCategory" → "mediaCategory"
    parts = _CAMEL_SPLIT_RE.sub("_", key).lower().split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def camelize(obj):
    if isinstance(obj, dict):
        return {_camel(k): camelize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [camelize(v) for v in obj]
    return obj

# ──────────────────────────────────────────────────────────────────────────────
# 🌐 SPARK — per-listing photos sub-resource
# ──────────────────────────────────────────────────────────────────────────────


def fetch_photos_for_listing(listing_key: str) -> tuple[list[dict] | None, str]:
    """
    Return (photos_list, status):
      - (non-empty list, "ok")        — photos fetched
      - ([],             "no_photos") — Spark returned empty (or 403/404)
      - (None,           "failed")    — network/HTTP error after retries
    """
    url = f"{BASE_URL}/{listing_key}/photos"
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            time.sleep(0.5 * (2 ** attempt))
            continue

        if res.status_code == 200:
            data = res.json().get("D", {})
            results = data.get("Results") or []
            if not results:
                return [], "no_photos"
            # Some payloads nest under StandardFields; some are flat photo dicts.
            out = []
            for r in results:
                base = r.get("StandardFields") or r
                out.append(camelize(base))
            return out, "ok"

        if res.status_code in (403, 404):
            # Listing has no photos accessible via replication feed
            return [], "no_photos"

        if res.status_code == 429:
            delay = 0.5 * (2 ** attempt)
            time.sleep(delay)
            continue

        if 500 <= res.status_code < 600:
            time.sleep(0.5 * (2 ** attempt))
            continue

        # Other 4xx — treat as "failed" but don't retry
        return None, "failed"

    return None, "failed"

# ──────────────────────────────────────────────────────────────────────────────
# 🧭 QUERY BUILDING
# ──────────────────────────────────────────────────────────────────────────────


def build_query(args) -> dict:
    q: dict = {}
    if args.mls:
        q["mlsSource"] = args.mls
    elif not args.all:
        # Neither --mls nor --all: default to --all-like behavior but warn.
        pass

    if args.force:
        return q

    if args.delta:
        # Fetch never-cached OR modified since last cache.
        q["$or"] = [
            {"photoCachedAt": {"$exists": False}},
            {"$expr": {"$gt": ["$modificationTimestamp", "$photoCachedAt"]}},
        ]
    else:
        q["photoCachedAt"] = {"$exists": False}

    return q

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Fetch per-listing photos from Spark and store on unified_listings.media.")
    parser.add_argument("--mls", choices=MLS_NAMES, help="Restrict to a single MLS.")
    parser.add_argument("--all", action="store_true", help="Process all MLSs (no mlsSource filter).")
    parser.add_argument("--delta", action="store_true",
                        help="Re-fetch docs where modificationTimestamp > photoCachedAt (daily cron mode).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore photoCachedAt — re-fetch every matched doc.")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help=f"Seconds between requests (default {DEFAULT_SLEEP}).")
    args = parser.parse_args()

    if not args.mls and not args.all:
        print("[ERROR] Must pass --mls <NAME> or --all.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client.get_database()

    # Iterate all three on-market lifecycle collections. Closed listings aren't
    # included — Spark strips photos after close so we preserve whatever media[]
    # was captured while the listing was alive.
    source_collections = ("unified_listings", "unified_in_escrow", "unified_off_market")

    query = build_query(args)
    projection = {"listingKey": 1, "mlsSource": 1, "modificationTimestamp": 1}

    docs: list[tuple[str, dict]] = []
    for src_name in source_collections:
        src = db[src_name]
        cursor = src.find(query, projection)
        if args.limit:
            cursor = cursor.limit(args.limit)
        for d in cursor:
            docs.append((src_name, d))
        if args.limit and len(docs) >= args.limit:
            docs = docs[: args.limit]
            break

    total = len(docs)
    if total == 0:
        print("[INFO] No listings to fetch. Nothing to do.")
        return

    print(f"[INFO] {total:,} listings to fetch photos for")
    print(f"[INFO] workers: 1")
    print(f"[INFO] mode: {'delta' if args.delta else ('force' if args.force else 'fresh')}")
    if args.mls:
        print(f"[INFO] mls: {args.mls}")
    # Breakdown by source
    from collections import Counter
    src_counts = Counter(src for src, _ in docs)
    for src, n in src_counts.most_common():
        print(f"        {src:<30} {n:,}")

    ok = 0
    no_photos = 0
    failed = 0
    total_photos = 0
    start_time = time.time()

    for i, (src_name, doc) in enumerate(docs, start=1):
        listing_key = str(doc.get("listingKey") or "").strip()
        if not listing_key:
            failed += 1
            continue

        photos, status = fetch_photos_for_listing(listing_key)
        now_iso = datetime.now(timezone.utc).isoformat()

        update: dict = {"$set": {"photoCachedAt": now_iso}}
        if status == "ok" and photos:
            update["$set"]["media"] = photos
            total_photos += len(photos)
            ok += 1
        elif status == "no_photos":
            # Mark cached so we don't retry every night; leave media untouched.
            no_photos += 1
        else:  # failed
            failed += 1
            # Still mark photoCachedAt so failures don't starve out successes
            # on repeat runs. The daily --delta will try again when the
            # listing is next modified.

        try:
            db[src_name].update_one({"listingKey": listing_key}, update)
        except Exception as e:
            print(f"  [DB] update failed for {listing_key} in {src_name}: {e}")

        if i % 500 == 0 or i == total:
            print(
                f"  [{i:>6,}/{total:<6,}]  "
                f"ok={ok:>6,}  no_photos={no_photos:>4,}  "
                f"failed={failed:>4,}  total_photos={total_photos:,}"
            )

        time.sleep(args.sleep)

    elapsed = time.time() - start_time
    print("\n[DONE]")
    print(f"  processed:    {total:,}")
    print(f"  ok:           {ok:,}")
    print(f"  no_photos:    {no_photos:,}")
    print(f"  failed:       {failed:,}")
    print(f"  total_photos: {total_photos:,}")
    if ok:
        print(f"  avg photos:   {total_photos / ok:.1f}")
    print(f"  elapsed:      {elapsed/60:.1f} min")

    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "delta" if args.delta else ("force" if args.force else "fresh"),
        "mls": args.mls,
        "all": args.all,
        "limit": args.limit,
        "processed": total,
        "ok": ok,
        "no_photos": no_photos,
        "failed": failed,
        "total_photos": total_photos,
        "avg_photos": round(total_photos / ok, 2) if ok else 0,
        "elapsed_seconds": round(elapsed, 1),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"fetch_photos_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  → {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user. photoCachedAt is per-doc so restart is safe.")
