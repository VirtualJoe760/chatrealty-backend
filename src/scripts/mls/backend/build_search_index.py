#!/usr/bin/env python3
"""
Build Search Index

Rebuilds the `search_index` collection used by the chat autocomplete. Tiny
denormalized docs + a Mongo $text index = sub-50ms typeahead across listings,
cities, and subdivisions without touching the heavy `unified_listings`
collection at request time.

Sources:
    unified_listings  → entries of type "listing"   (Active residential only)
    cities            → entries of type "city"
    subdivisions      → entries of type "subdivision" (listingCount > 0)

Strategy:
    1. Stamp every entry with this run's `updatedAt`.
    2. Bulk upsert by (type, entityId).
    3. Delete anything older than this run's start time — that's the cleanup.

Usage:
    python3 src/scripts/mls/backend/build_search_index.py
    python3 src/scripts/mls/backend/build_search_index.py --dry-run
    python3 src/scripts/mls/backend/build_search_index.py --skip-cleanup   # don't prune stale
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne, DESCENDING, ASCENDING, TEXT

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client["admin"]

# ──────────────────────────────────────────────────────────────────────────────
# 📐 CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

BATCH = 1000

# subdivisionName values that aren't real subdivisions — skip from listing
# searchText and skip outright when iterating the subdivisions collection.
EXCLUDED_SUBDIVISIONS = {
    "not applicable", "n/a", "none", "other", "na", "no hoa",
    "see remarks", "unknown", "tbd", "various", "",
}


def _is_real_subdivision(name) -> bool:
    if not name or not isinstance(name, str):
        return False
    return name.strip().lower() not in EXCLUDED_SUBDIVISIONS


def _norm(s) -> str:
    return (s or "").strip().lower() if isinstance(s, (str,)) else ""


# ──────────────────────────────────────────────────────────────────────────────
# 🏗️ ENTRY BUILDERS
# ──────────────────────────────────────────────────────────────────────────────


def build_listing_entry(doc: dict, run_at: datetime) -> dict | None:
    listing_key = doc.get("listingKey")
    if not listing_key:
        return None

    address = doc.get("unparsedAddress") or ""
    city = doc.get("city") or ""
    subdivision = doc.get("subdivisionName")
    postal = doc.get("postalCode") or ""
    street_name = doc.get("streetName") or ""

    sub_clean = subdivision if _is_real_subdivision(subdivision) else None

    # searchText: full address + city + subdivision (if real) + zip + bare street name
    parts = [_norm(address), _norm(city)]
    if sub_clean:
        parts.append(_norm(sub_clean))
    if postal:
        parts.append(_norm(postal))
    if street_name:
        parts.append(_norm(street_name))
    search_text = " ".join(p for p in parts if p)

    # baths: prefer the decimal field; fall back to whole-number bathsTotal
    baths = doc.get("bathroomsTotalDecimal") or doc.get("bathsTotal")
    try:
        baths = float(baths) if baths is not None else None
    except (TypeError, ValueError):
        baths = None

    # beds & sqft: defensive int conversion
    def _safe_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "type": "listing",
        "entityId": listing_key,
        "label": address or f"{street_name}, {city}".strip(", "),
        "sublabel": sub_clean or city or "",
        "searchText": search_text,
        "slug": doc.get("slugAddress"),
        "price": _safe_int(doc.get("listPrice")),
        "beds": _safe_int(doc.get("bedsTotal") or doc.get("bedroomsTotal")),
        "baths": baths,
        "sqft": _safe_int(doc.get("livingArea")),
        "photo": None,  # primaryPhotoUrl is not on unified_listings; see note
        "city": city or None,
        "subdivision": sub_clean,
        "status": doc.get("standardStatus"),
        "totalListings": None,
        "parentCity": None,
        "updatedAt": run_at,
    }


def build_city_entry(doc: dict, run_at: datetime) -> dict | None:
    name = doc.get("name")
    slug = doc.get("slug")
    if not name or not slug:
        return None

    # The cities collection's count field is `listingCount`, not `totalListings`.
    count = doc.get("listingCount") or doc.get("totalListings") or 0

    return {
        "type": "city",
        "entityId": slug,
        "label": name,
        "sublabel": f"{count:,} active listings" if count else "city",
        "searchText": _norm(name),
        "slug": slug,
        "price": None, "beds": None, "baths": None, "sqft": None, "photo": None,
        "city": name,
        "subdivision": None,
        "status": None,
        "totalListings": int(count),
        "parentCity": None,
        "updatedAt": run_at,
    }


def build_subdivision_entry(doc: dict, run_at: datetime) -> dict | None:
    name = doc.get("name")
    slug = doc.get("slug")
    city = doc.get("city") or ""
    if not name or not slug:
        return None
    if not _is_real_subdivision(name):
        return None

    return {
        "type": "subdivision",
        "entityId": slug,
        "label": name,
        "sublabel": city or "subdivision",
        "searchText": f"{_norm(name)} {_norm(city)}".strip(),
        "slug": slug,
        "price": None, "beds": None, "baths": None, "sqft": None, "photo": None,
        "city": city or None,
        "subdivision": name,
        "status": None,
        "totalListings": None,
        "parentCity": city or None,
        "updatedAt": run_at,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Rebuild the search_index collection.")
    parser.add_argument("--dry-run", action="store_true", help="Compute entries but don't write.")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Skip deleting stale entries (older than this run).")
    args = parser.parse_args()

    started = time.time()
    run_at = datetime.now(timezone.utc)
    coll = db["search_index"]
    print(f"[SearchIndex] Starting build... (dry-run={args.dry_run})")

    # ── Listings ──────────────────────────────────────────────────────────────
    listing_cursor = db["unified_listings"].find(
        {"standardStatus": "Active", "propertyType": "A"},
        {
            "listingKey": 1, "slugAddress": 1, "unparsedAddress": 1,
            "listPrice": 1,
            "bedsTotal": 1, "bedroomsTotal": 1,
            "bathroomsTotalDecimal": 1, "bathsTotal": 1,
            "livingArea": 1,
            "city": 1, "subdivisionName": 1, "postalCode": 1,
            "standardStatus": 1, "streetName": 1, "streetNumber": 1,
            "_id": 0,
        },
    )

    listing_entries = []
    for doc in listing_cursor:
        entry = build_listing_entry(doc, run_at)
        if entry:
            listing_entries.append(entry)
    print(f"[SearchIndex] Found {len(listing_entries):,} active listings")

    # ── Cities ────────────────────────────────────────────────────────────────
    city_entries = []
    for doc in db["cities"].find({}, {"name": 1, "slug": 1, "listingCount": 1, "totalListings": 1, "_id": 0}):
        entry = build_city_entry(doc, run_at)
        if entry:
            city_entries.append(entry)
    print(f"[SearchIndex] Found {len(city_entries):,} cities")

    # ── Subdivisions ──────────────────────────────────────────────────────────
    sub_entries = []
    for doc in db["subdivisions"].find(
        {"listingCount": {"$gt": 0}},
        {"name": 1, "slug": 1, "city": 1, "listingCount": 1, "_id": 0},
    ):
        entry = build_subdivision_entry(doc, run_at)
        if entry:
            sub_entries.append(entry)
    print(f"[SearchIndex] Found {len(sub_entries):,} subdivisions with listings")

    all_entries = listing_entries + city_entries + sub_entries
    print(f"[SearchIndex] Built {len(all_entries):,} search entries")

    if args.dry_run:
        print("[SearchIndex] Dry run — no writes")
        # Show a small sample so you can eyeball the shape
        for sample in (listing_entries[:1] + city_entries[:1] + sub_entries[:1]):
            import json
            print(json.dumps(sample, indent=2, default=str))
        return

    # ── Bulk upsert by (type, entityId) ───────────────────────────────────────
    operations = [
        UpdateOne(
            {"type": e["type"], "entityId": e["entityId"]},
            {"$set": e},
            upsert=True,
        )
        for e in all_entries
    ]

    upsert_started = time.time()
    batches = 0
    upserted = modified = 0
    for i in range(0, len(operations), BATCH):
        chunk = operations[i:i + BATCH]
        res = coll.bulk_write(chunk, ordered=False)
        batches += 1
        upserted += res.upserted_count
        modified += res.modified_count
    print(
        f"[SearchIndex] Upserted in {batches} batches "
        f"(new={upserted:,}, updated={modified:,}, {time.time()-upsert_started:.2f}s)"
    )

    # ── Cleanup stale entries (anything not stamped by this run) ──────────────
    if not args.skip_cleanup:
        del_res = coll.delete_many({"updatedAt": {"$lt": run_at}})
        print(f"[SearchIndex] Cleaned {del_res.deleted_count:,} stale entries")

    # ── Indexes ───────────────────────────────────────────────────────────────
    existing = {idx["name"] for idx in coll.list_indexes()}
    if "search_text_idx" not in existing:
        coll.create_index([("searchText", TEXT)], name="search_text_idx")
        print("[SearchIndex] Created search_text_idx")
    if "type_price_idx" not in existing:
        coll.create_index([("type", ASCENDING), ("price", DESCENDING)], name="type_price_idx")
        print("[SearchIndex] Created type_price_idx")
    print("[SearchIndex] Indexes verified")

    print(f"[SearchIndex] Done in {time.time()-started:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
