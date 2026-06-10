#!/usr/bin/env python3
"""
Unified Status Update — batched full-data refresher + cross-collection router.

This is the canonical daily freshener. It walks every listingKey across the
three on-market lifecycle collections (unified_listings, unified_in_escrow,
unified_off_market), queries Spark in batches of 25 with full StandardFields,
flattens each response, and routes the fresh doc to the correct collection
based on its current status.

Why this shape:
  - Spark's ModificationTimestamp can lag or get stripped on certain field
    changes (happens anecdotally with price corrections, description edits).
    Relying on the incremental window alone leaves stale data in our DB.
  - Batching 25 keys per request is ~18× fewer API calls than the prior
    one-at-a-time minimal-payload design, while returning full data instead
    of just a status field.
  - The cross-collection router handles transitions cleanly: Active →
    Pending moves the doc from unified_listings into unified_in_escrow and
    deletes the stale copy.

Runtime target: ~30 min/day for ~122k on-market docs at ~3 req/sec.

Usage:
    # Routine daily run (7am cron)
    python3 src/scripts/mls/backend/unified/update-status.py

    # Restrict to one source collection
    python3 src/scripts/mls/backend/unified/update-status.py --source unified_in_escrow

    # Restrict to one MLS (testing)
    python3 src/scripts/mls/backend/unified/update-status.py --mls ITECH

    # Dry-run: show what would change, no DB writes
    python3 src/scripts/mls/backend/unified/update-status.py --dry-run
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

# Sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flatten import flatten_listing  # noqa: E402
from seed import STATUS_ROUTES, ALL_LIFECYCLE_COLLECTIONS, route_status  # noqa: E402

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

# Must mirror unified-fetch.py / flatten.py
MLS_IDS = {
    "GPS": "20190211172710340762000000",
    "CRMLS": "20200218121507636729000000",
    "CLAW": "20200630203341057545000000",
    "SOUTHLAND": "20200630203518576361000000",
    "HIGH_DESERT": "20200630204544040064000000",
    "BRIDGE": "20200630204733042221000000",
    "CONEJO_SIMI_MOORPARK": "20160622112753445171000000",
    "ITECH": "20200630203206752718000000",
}

# On-market source collections we reconcile from. Closed is a terminal state —
# once a doc lives in unified_closed_listings we don't re-query Spark for it.
SOURCE_COLLECTIONS = ("unified_listings", "unified_in_escrow", "unified_off_market")

BATCH_SIZE = 25              # Spark listingKeys per request
REQUEST_SLEEP = 0.3          # ~3 req/sec
RATE_LIMIT_BACKOFF = (3, 6, 12)
MAX_ATTEMPTS = 4
MISS_ESCALATION_THRESHOLD = 2   # 2nd consecutive Spark miss → move to off_market

LOG_DIR = Path(__file__).resolve().parents[5] / "local-logs" / "status-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# 🌐 SPARK — batched full-data fetch
# ──────────────────────────────────────────────────────────────────────────────

# MLSs known to reject _expand=Media. Populated lazily on first 400 response.
_NO_MEDIA_MLS: set[str] = set()


def _build_url(mls_id: str, keys: list[str], include_media: bool) -> str:
    key_clause = " Or ".join(f"ListingKey Eq '{k}'" for k in keys)
    raw_filter = f"MlsId Eq '{mls_id}' And ({key_clause})"
    safe_chars = " '()="
    encoded_filter = requests.utils.quote(raw_filter, safe=safe_chars)
    expansions = ["OpenHouses", "VirtualTours"]
    if include_media:
        expansions.insert(0, "Media")
    params = [
        f"_filter={encoded_filter}",
        f"_expand={','.join(expansions)}",
        f"_limit={BATCH_SIZE}",
    ]
    return f"{BASE_URL}?{'&'.join(params)}"


def fetch_batch(mls_name: str, mls_id: str, keys: list[str]) -> list[dict]:
    """Return Spark StandardFields dicts for these listingKeys. Missing keys are simply absent."""
    include_media = mls_name not in _NO_MEDIA_MLS

    for attempt in range(MAX_ATTEMPTS):
        url = _build_url(mls_id, keys, include_media)
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            if attempt == MAX_ATTEMPTS - 1:
                print(f"    [NET] {mls_name} batch giving up: {e}")
                return []
            time.sleep(2 + attempt)
            continue

        if res.status_code == 200:
            return res.json().get("D", {}).get("Results", [])

        if res.status_code == 400 and include_media and "Media" in res.text:
            _NO_MEDIA_MLS.add(mls_name)
            include_media = False
            continue

        if res.status_code == 429:
            wait = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
            print(f"    [429] {mls_name} rate limited, sleeping {wait}s")
            time.sleep(wait)
            continue

        print(f"    [HTTP {res.status_code}] {res.text[:160]}")
        return []

    return []

# ──────────────────────────────────────────────────────────────────────────────
# 🧹 LEGACY STUCK-CLOSED SWEEP (kept from prior version)
# ──────────────────────────────────────────────────────────────────────────────


def sweep_stuck_closed(db, dry_run: bool = False) -> dict:
    """Move any on-market doc whose standardStatus is already 'Closed' into unified_closed_listings."""
    closed_coll = db.unified_closed_listings
    total_moved = 0
    total_found = 0
    errors = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for src_name in SOURCE_COLLECTIONS:
        src = db[src_name]
        stuck = list(src.find({"standardStatus": "Closed"}))
        if not stuck:
            continue
        total_found += len(stuck)
        print(f"🧹 sweep: {src_name} has {len(stuck):,} stuck Closed docs")

        if dry_run:
            continue

        for doc in stuck:
            key = doc.get("listingKey")
            if not key:
                errors += 1
                continue
            doc.pop("_id", None)
            doc["statusLastChecked"] = now_iso
            doc["movedToClosedAt"] = now_iso
            try:
                closed_coll.update_one(
                    {"listingKey": key},
                    {"$set": doc},
                    upsert=True,
                )
                src.delete_one({"listingKey": key})
                total_moved += 1
            except Exception as e:
                errors += 1
                print(f"    [ERR] {key}: {e}")

    print(f"🧹 sweep complete: found={total_found:,}  moved={total_moved:,}  errors={errors:,}")
    return {"found": total_found, "moved": total_moved, "errors": errors}

# ──────────────────────────────────────────────────────────────────────────────
# 📋 COLLECTION LOOKUP
# ──────────────────────────────────────────────────────────────────────────────


def _find_doc_in_any_source(db, listing_key: str) -> tuple[str | None, dict | None]:
    """Return (collection_name, doc) if the key lives in any source collection."""
    for src_name in SOURCE_COLLECTIONS:
        doc = db[src_name].find_one({"listingKey": listing_key})
        if doc:
            return src_name, doc
    return None, None


def _apply_update(db, listing_key: str, flat: dict, source: str,
                   target: str, dry_run: bool) -> str:
    """Upsert flat into target, delete from source if different. Returns action label."""
    now_iso = datetime.now(timezone.utc).isoformat()
    flat = dict(flat)
    flat.pop("_id", None)
    flat["statusLastChecked"] = now_iso
    flat["lastSparkRefreshAt"] = now_iso
    flat.pop("lastSparkMissAt", None)  # previous miss resolved

    if target == "unified_closed_listings":
        flat["movedToClosedAt"] = flat.get("movedToClosedAt") or now_iso

    if dry_run:
        return "move" if source != target else "refresh"

    db[target].update_one(
        {"listingKey": listing_key},
        {"$set": flat},
        upsert=True,
    )
    if source != target:
        db[source].delete_one({"listingKey": listing_key})
        return "move"
    return "refresh"


def _mark_missed(db, listing_key: str, source: str, dry_run: bool) -> str:
    """
    Spark didn't return this key. Bump lastSparkMissAt; on the 2nd consecutive
    miss, move to unified_off_market (listing appears to be gone).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = db[source].find_one({"listingKey": listing_key}, {"lastSparkMissAt": 1, "sparkMissCount": 1})
    if not doc:
        return "gone"
    miss_count = (doc.get("sparkMissCount") or 0) + 1

    if miss_count >= MISS_ESCALATION_THRESHOLD and source != "unified_off_market":
        if dry_run:
            return "escalate"
        full = db[source].find_one({"listingKey": listing_key})
        if full:
            full.pop("_id", None)
            full["standardStatus"] = full.get("standardStatus") or "OffMarket"
            full["lastSparkMissAt"] = now_iso
            full["sparkMissCount"] = miss_count
            full["movedToOffMarketAt"] = now_iso
            db.unified_off_market.update_one(
                {"listingKey": listing_key},
                {"$set": full},
                upsert=True,
            )
            db[source].delete_one({"listingKey": listing_key})
        return "escalate"

    if dry_run:
        return "miss"
    db[source].update_one(
        {"listingKey": listing_key},
        {"$set": {
            "lastSparkMissAt": now_iso,
            "sparkMissCount": miss_count,
            "statusLastChecked": now_iso,
        }},
    )
    return "miss"

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Batched full-data status reconciler across lifecycle collections.")
    parser.add_argument("--source", choices=list(SOURCE_COLLECTIONS),
                        help="Restrict to one source collection (default: all three).")
    parser.add_argument("--mls", choices=list(MLS_IDS.keys()),
                        help="Restrict to one MLS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change. No DB writes.")
    parser.add_argument("--no-sweep", action="store_true",
                        help="Skip the stuck-Closed sweep.")
    parser.add_argument("--sweep-only", action="store_true",
                        help="Only run the stuck-Closed sweep, skip Spark reconcile.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Spark listingKeys per request (default {BATCH_SIZE}).")
    args = parser.parse_args()

    if args.sweep_only and args.no_sweep:
        print("[ERROR] --sweep-only and --no-sweep are mutually exclusive.")
        sys.exit(1)

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print("=" * 80)
    print(f"Unified Status Update — mode: {mode}")
    print("=" * 80)

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client.get_database()

    # ── Phase 1: stuck-Closed sweep ───────────────────────────────────────────
    if not args.no_sweep:
        sweep_result = sweep_stuck_closed(db, dry_run=args.dry_run)
    else:
        sweep_result = {"found": 0, "moved": 0, "errors": 0}

    if args.sweep_only:
        print("\n--sweep-only: exiting.")
        return

    # ── Phase 2: gather all on-market listingKeys, grouped by (mls, source) ──
    sources = [args.source] if args.source else list(SOURCE_COLLECTIONS)
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    total_keys = 0
    for src_name in sources:
        proj = {"listingKey": 1, "mlsSource": 1}
        query: dict = {}
        if args.mls:
            query["mlsSource"] = args.mls
        for doc in db[src_name].find(query, proj):
            mls = doc.get("mlsSource")
            key = doc.get("listingKey")
            if mls in MLS_IDS and key:
                buckets[(mls, src_name)].append(str(key))
                total_keys += 1

    if total_keys == 0:
        print("[INFO] No on-market docs to reconcile. Nothing to do.")
        return

    print(f"\n[INFO] {total_keys:,} listingKeys across {len(buckets)} (mls, source) buckets")

    # ── Phase 3: batched reconcile ────────────────────────────────────────────
    actions: Counter = Counter()
    status_changes: Counter = Counter()
    route_hits: Counter = Counter()
    errors = 0
    started = time.time()
    checked = 0

    for (mls_name, src_name), keys in buckets.items():
        mls_id = MLS_IDS[mls_name]
        print(f"\n>>> [{mls_name} / {src_name}]  {len(keys):,} keys")

        for i in range(0, len(keys), args.batch_size):
            batch_keys = keys[i : i + args.batch_size]
            try:
                results = fetch_batch(mls_name, mls_id, batch_keys)
            except Exception as e:
                errors += 1
                print(f"    [ERR] batch failed: {e}")
                time.sleep(REQUEST_SLEEP)
                continue

            returned_by_key: dict[str, dict] = {}
            for raw in results:
                sf = raw.get("StandardFields") or raw
                k = str(sf.get("ListingKey", "")).strip()
                if k:
                    returned_by_key[k] = raw

            for key in batch_keys:
                checked += 1
                raw = returned_by_key.get(key)
                if not raw:
                    # Spark didn't return this key — mark miss / escalate
                    action = _mark_missed(db, key, src_name, args.dry_run)
                    actions[action] += 1
                    continue

                try:
                    flat = flatten_listing(raw)
                except Exception as e:
                    errors += 1
                    print(f"    [FLAT] {key}: {e}")
                    continue
                if not flat:
                    errors += 1
                    continue

                new_status = flat.get("standardStatus")
                target = route_status(new_status)
                route_hits[target] += 1

                if src_name != target:
                    status_changes[(src_name, target)] += 1

                action = _apply_update(db, key, flat, src_name, target, args.dry_run)
                actions[action] += 1

            batch_num = (i // args.batch_size) + 1
            if batch_num % 20 == 0 or i + args.batch_size >= len(keys):
                elapsed = time.time() - started
                rate = checked / elapsed if elapsed > 0 else 0
                print(f"    batch {batch_num:4d}  checked={checked:,}  "
                      f"rate={rate:.1f}/s  moves={actions.get('move',0):,}  "
                      f"refreshes={actions.get('refresh',0):,}  misses={actions.get('miss',0):,}")

            time.sleep(REQUEST_SLEEP)

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.time() - started
    print("\n" + "=" * 80)
    print("RECONCILE SUMMARY")
    print("=" * 80)
    print(f"Mode:           {mode}")
    print(f"Checked:        {checked:,}")
    print(f"Refreshes:      {actions.get('refresh', 0):,}")
    print(f"Moves:          {actions.get('move', 0):,}")
    print(f"Misses (1st):   {actions.get('miss', 0):,}")
    print(f"Escalated:      {actions.get('escalate', 0):,}")
    print(f"Errors:         {errors:,}")
    print(f"Elapsed:        {elapsed/60:.1f} min")

    if status_changes:
        print("\nTop collection moves (src → target):")
        for (src, tgt), count in status_changes.most_common(10):
            print(f"  {src:<30} → {tgt:<30} {count:>6,}")

    if route_hits:
        print("\nRouting hits by target (where the fresh data landed):")
        for tgt, count in sorted(route_hits.items(), key=lambda x: -x[1]):
            print(f"  {tgt:<30} {count:>6,}")

    log = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "sweep": sweep_result,
        "checked": checked,
        "actions": dict(actions),
        "status_changes": {f"{s} → {t}": n for (s, t), n in status_changes.items()},
        "route_hits": dict(route_hits),
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
        "no_media_mls": sorted(_NO_MEDIA_MLS),
    }
    log_path = LOG_DIR / f"status_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, default=str)
    print(f"\n🪵 Log: {log_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user. Safe to restart — already-checked docs have statusLastChecked set.")
