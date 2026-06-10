#!/usr/bin/env python3
"""
Unified MLS Seed — Status Router

Reads a flattened listings JSON and writes each doc to the correct collection
based on its current `standardStatus`:

    Active / ComingSoon           → unified_listings
    Pending / Active Under Contract → unified_in_escrow
    Hold / Expired / Canceled /
      Withdrawn / OffMarket       → unified_off_market
    Closed                         → unified_closed_listings

When a listing's status has changed since the previous run, the router also
DELETES its listingKey from the other three collections — so a listing that
flips Active → Pending moves cleanly from unified_listings into
unified_in_escrow without duplicating.

Features:
- Bulk upsert operations (500 per batch) per target collection
- Cross-collection cleanup (bulk delete by listingKey)
- Geospatial + compound index creation for every lifecycle collection
- Progress tracking per target

Usage:
    # Seed from auto-detected flattened file (routes by status)
    python src/scripts/mls/backend/unified/seed.py

    # Seed from specific file
    python src/scripts/mls/backend/unified/seed.py --input local-logs/flattened_unified_GPS_listings.json

    # Create/refresh indexes on all lifecycle collections
    python src/scripts/mls/backend/unified/seed.py --indexes-only
"""

import os
import json
import time
import argparse
from collections import defaultdict
from pathlib import Path
from pymongo import MongoClient, UpdateOne, GEOSPHERE, ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError, ConnectionFailure, ServerSelectionTimeoutError
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("[ERROR] MONGODB_URI is not set in .env.local")

# ──────────────────────────────────────────────────────────────────────────────
# 🧭 STATUS ROUTER
# ──────────────────────────────────────────────────────────────────────────────

STATUS_ROUTES = {
    # On-market — publicly displayed
    "Active":                  "unified_listings",
    "ComingSoon":              "unified_listings",
    # In escrow — real transaction in progress
    "Pending":                 "unified_in_escrow",
    "Active Under Contract":   "unified_in_escrow",
    # Off-market — temp paused or terminated without sale
    "Hold":                    "unified_off_market",
    "Expired":                 "unified_off_market",
    "Canceled":                "unified_off_market",
    "Cancelled":               "unified_off_market",   # UK spelling variant
    "Withdrawn":               "unified_off_market",
    "OffMarket":               "unified_off_market",
    # Actually sold
    "Closed":                  "unified_closed_listings",
}

ALL_LIFECYCLE_COLLECTIONS = set(STATUS_ROUTES.values())
DEFAULT_COLLECTION = "unified_listings"


def route_status(status: str | None) -> str:
    """Return which collection this status belongs in. Unknown → default."""
    if not status:
        return DEFAULT_COLLECTION
    return STATUS_ROUTES.get(status.strip(), DEFAULT_COLLECTION)

# ──────────────────────────────────────────────────────────────────────────────
# 🔌 MongoDB
# ──────────────────────────────────────────────────────────────────────────────


def connect_to_mongodb(retries=3):
    for attempt in range(retries):
        try:
            client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=10000,
                socketTimeoutMS=20000,
            )
            client.admin.command("ping")
            db = client.get_database()
            print("[OK] Connected to MongoDB")
            return client, db
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            if attempt == retries - 1:
                raise Exception(f"[ERROR] Failed to connect to MongoDB after {retries} attempts: {e}")
            print(f"[WARN] MongoDB connection attempt {attempt + 1} failed, retrying...")
            time.sleep(2 ** attempt)

# ──────────────────────────────────────────────────────────────────────────────
# 🗂️ INDEXES (applied to every lifecycle collection)
# ──────────────────────────────────────────────────────────────────────────────


def create_indexes(collection, label: str):
    """Create core operational indexes on one lifecycle collection."""
    print(f"\n>>> Creating indexes on {label}...")
    specs = [
        ([("listingKey", ASCENDING)], {"name": "listingKey_unique", "unique": True}),
        ([("mlsSource", ASCENDING), ("mlsId", ASCENDING)], {"name": "mlsSource_mlsId"}),
        ([("standardStatus", ASCENDING)], {"name": "standardStatus_1"}),
        ([("modificationTimestamp", DESCENDING)], {"name": "modificationTimestamp_desc"}),
        ([("coordinates", GEOSPHERE)], {"name": "coordinates_2dsphere"}),
        ([("city", ASCENDING), ("standardStatus", ASCENDING)], {"name": "city_status"}),
        ([("subdivisionName", ASCENDING), ("standardStatus", ASCENDING)], {"name": "subdivision_status"}),
        ([("propertyType", ASCENDING), ("standardStatus", ASCENDING)], {"name": "propertyType_status"}),
        ([("statusLastChecked", ASCENDING)], {"name": "statusLastChecked_1"}),
        ([("lastSparkMissAt", ASCENDING)], {"name": "lastSparkMissAt_1"}),
    ]
    for keys, opts in specs:
        try:
            collection.create_index(keys, **opts)
        except Exception as e:
            print(f"  [WARN] {label}.{opts['name']}: {e}")
    print(f"[OK] Indexes ensured on {label}")

# ──────────────────────────────────────────────────────────────────────────────
# 🌱 SEED / ROUTE
# ──────────────────────────────────────────────────────────────────────────────


def simple_slugify(s: str) -> str:
    return (
        s.lower()
        .replace(",", "")
        .replace(".", "")
        .replace("/", "-")
        .replace(" ", "-")
        .strip()
    )


def _normalize(raw: dict) -> tuple[dict, bool]:
    """Normalize required fields. Returns (raw, ok)."""
    listing_key = raw.get("listingKey")
    address = raw.get("unparsedAddress")
    slug = raw.get("slug") or listing_key
    if not listing_key or not address or not slug:
        return raw, False
    raw["listingKey"] = listing_key
    raw["slug"] = slug
    raw["slugAddress"] = raw.get("slugAddress") or simple_slugify(address)

    lat = raw.get("latitude"); lng = raw.get("longitude")
    if lat and lng:
        try:
            raw["coordinates"] = {
                "type": "Point",
                "coordinates": [float(lng), float(lat)],
            }
        except (ValueError, TypeError):
            pass
    raw.pop("_id", None)
    return raw, True


def route_and_seed(input_file: Path, db) -> dict:
    """
    Load flattened listings, group by target collection (based on status),
    bulk-upsert each group, then bulk-delete those listingKeys from the
    other lifecycle collections.
    """
    if not input_file.exists():
        raise Exception(f"[ERROR] Input file {input_file} does not exist")

    print(f">>> Loading flattened listings from {input_file}")
    with open(input_file, encoding="utf-8") as f:
        listings = json.load(f)
    print(f">>> Processing {len(listings):,} listings...")

    by_target: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for raw in listings:
        raw, ok = _normalize(raw)
        if not ok:
            skipped += 1
            continue
        target = route_status(raw.get("standardStatus"))
        by_target[target].append(raw)

    print("\n>>> Routing summary (pre-write):")
    for target, docs in sorted(by_target.items()):
        print(f"  {target:<30} {len(docs):>6,}")
    if skipped:
        print(f"  (skipped: {skipped} docs missing required fields)")

    report = {t: {"upserted": 0, "modified": 0, "failed": 0, "evicted_from_others": 0}
              for t in by_target}

    for target_name, docs in by_target.items():
        target_coll = db[target_name]
        print(f"\n>>> {target_name}: upserting {len(docs):,} docs")

        ops = [
            UpdateOne({"listingKey": d["listingKey"]}, {"$set": d}, upsert=True)
            for d in docs
        ]

        batch_size = 500
        for i in range(0, len(ops), batch_size):
            chunk = ops[i : i + batch_size]
            batch_num = i // batch_size + 1
            try:
                result = target_coll.bulk_write(chunk, ordered=False)
                report[target_name]["modified"] += result.modified_count or 0
                report[target_name]["upserted"] += result.upserted_count or 0
                print(f"  [Batch {batch_num}] modified={result.modified_count or 0}  upserted={result.upserted_count or 0}")
            except BulkWriteError as e:
                errs = len(e.details.get("writeErrors", [])) if e.details else 1
                report[target_name]["failed"] += errs
                print(f"  [Batch {batch_num}] {errs} errors")
            except Exception as e:
                raise Exception(f"[ERROR] {target_name} batch {batch_num}: {e}")

        # Evict from every other lifecycle collection — handles status transitions
        target_keys = [d["listingKey"] for d in docs]
        for other in ALL_LIFECYCLE_COLLECTIONS - {target_name}:
            if other not in db.list_collection_names():
                continue
            del_result = db[other].delete_many({"listingKey": {"$in": target_keys}})
            report[target_name]["evicted_from_others"] += del_result.deleted_count
            if del_result.deleted_count:
                print(f"  ⇄ removed {del_result.deleted_count:,} stale copies from {other}")

    # Totals
    total_upserted = sum(r["upserted"] for r in report.values())
    total_modified = sum(r["modified"] for r in report.values())
    total_failed = sum(r["failed"] for r in report.values())
    total_evicted = sum(r["evicted_from_others"] for r in report.values())

    print("\n[OK] Seed complete")
    print(f"    Upserted:         {total_upserted:,}")
    print(f"    Modified:         {total_modified:,}")
    print(f"    Failed:           {total_failed:,}")
    print(f"    Evicted elsewhere:{total_evicted:,}   (listings that changed collection)")
    print(f"    Skipped:          {skipped:,}")

    return {
        "report": report,
        "totals": {
            "upserted": total_upserted,
            "modified": total_modified,
            "failed": total_failed,
            "evicted": total_evicted,
            "skipped": skipped,
        },
    }

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Seed flattened MLS listings into the correct lifecycle collection (router).")
    parser.add_argument("--input", type=str,
                        help="Input JSON file path (default: auto-detect newest flattened_unified_*.json in local-logs).")
    parser.add_argument("--indexes-only", action="store_true",
                        help="Create/refresh indexes on every lifecycle collection, then exit.")
    args = parser.parse_args()

    try:
        print("=" * 80)
        print("Unified MLS Seed — Status Router")
        print("=" * 80)

        client, db = connect_to_mongodb()

        # Ensure indexes exist on every lifecycle collection we might write to
        for coll_name in sorted(ALL_LIFECYCLE_COLLECTIONS):
            create_indexes(db[coll_name], coll_name)

        if args.indexes_only:
            print("[OK] Indexes ensured on all lifecycle collections. Exiting (--indexes-only).")
            return

        # Auto-detect input file
        if args.input:
            input_path = Path(args.input)
        else:
            project_root = Path(__file__).resolve().parents[5]
            local_logs = project_root / "local-logs"
            candidates = sorted(
                local_logs.glob("flattened_unified_*_listings.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise Exception("[ERROR] No flattened files found. Run flatten.py first.")
            input_path = candidates[0]

        result = route_and_seed(input_path, db)

        print("\n" + "=" * 80)
        print("Summary:")
        print(f"  Input: {input_path}")
        for target, r in sorted(result["report"].items()):
            print(f"  {target:<30} upserted={r['upserted']:>6,}  modified={r['modified']:>6,}  "
                  f"failed={r['failed']:>5,}  evicted_from_others={r['evicted_from_others']:>6,}")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        exit(1)


if __name__ == "__main__":
    main()
