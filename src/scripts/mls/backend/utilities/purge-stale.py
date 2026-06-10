#!/usr/bin/env python3
"""
Purge Stale Listings (safety-guarded rewrite)

For each MLS:
  1. Query Spark for every currently-accessible Active listingKey
     (paginated, `_select=ListingKey`, same filter shape as unified-fetch.py).
  2. Read every `unified_listings` doc currently marked Active for that MLS.
  3. Compute stale = DB-active keys NOT present in Spark's accessible set.
  4. Evaluate safety guards — abort the MLS if ANY guard trips:
       * Spark returned implausibly few keys (absolute floor)
       * Spark's count is much smaller than the DB's active count (ratio floor)
       * Proposed purge is too many docs absolute
       * Proposed purge is too high a % of DB-active
  5. If guards pass and --apply is set:
       update_many({listingKey: {$in: stale}}, $set: {
           standardStatus: "OffMarket",
           statusLastChecked: <now>,
           purgedAt:         <now>,
       })

Dry-run is the default. No writes unless --apply.

Background: on 2026-04-06 06:00 UTC, a previous version of this script
purged 68,887 listings in a single run across 7 MLSs (CRMLS 45,771,
CLAW 12,127, SOUTHLAND 6,415, HIGH_DESERT 2,659, BRIDGE 1,242, CONEJO
655, ITECH 18) because Spark transiently returned a tiny accessible
set for those MLSs while the DB still had tens of thousands Active.
The guards below are calibrated so that event would have aborted every
affected MLS without writing a single doc. Paired with
restore-purged-listings.py for recovery.

Usage:
    # Dry-run across all MLSs
    python3 src/scripts/mls/backend/unified/purge-stale.py

    # Dry-run one MLS
    python3 src/scripts/mls/backend/unified/purge-stale.py --mls GPS

    # Apply for real
    python3 src/scripts/mls/backend/unified/purge-stale.py --mls GPS --apply

    # Tune a threshold
    python3 src/scripts/mls/backend/unified/purge-stale.py --max-purge-ratio 0.15 --apply
"""

import os
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

PROPERTY_TYPES = ["A", "B", "C", "D"]  # Residential, Lease, Multi-family, Land
PAGE_SIZE = 1000

# ── SAFETY-GUARD DEFAULTS ─────────────────────────────────────────────────────
# These are deliberately conservative. The Apr 6 incident had CRMLS Spark
# returning essentially nothing while DB had 45,771 Active — that trips
# both MIN_SPARK_ABSOLUTE and MIN_SPARK_RATIO with room to spare.
DEFAULT_MIN_SPARK_ABSOLUTE = 100       # reject if Spark returned < this many keys for an MLS
DEFAULT_MIN_SPARK_RATIO = 0.50         # reject if Spark count < DB_active × this
DEFAULT_MAX_PURGE_ABSOLUTE = 10_000    # reject if we'd purge more than this many
DEFAULT_MAX_PURGE_RATIO = 0.30         # reject if we'd purge > this % of DB-active
# MLSs with fewer than this many DB actives skip the ratio guards (small MLSs
# like ITECH have tiny absolute counts and natural churn can look like a big %).
SMALL_MLS_FLOOR = 50

LOG_DIR = Path(__file__).resolve().parents[5] / "local-logs" / "purge-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# 🌐 SPARK — fetch all accessible Active listingKeys for an MLS
# ──────────────────────────────────────────────────────────────────────────────


def _spark_filter(mls_id: str) -> str:
    type_clause = "(" + " Or ".join(f"PropertyType Eq '{t}'" for t in PROPERTY_TYPES) + ")"
    return f"MlsId Eq '{mls_id}' And {type_clause} And StandardStatus Eq 'Active'"


def spark_total_count(filter_query: str) -> int | None:
    safe_chars = " '()="
    encoded = requests.utils.quote(filter_query, safe=safe_chars)
    url = f"{BASE_URL}?_filter={encoded}&_pagination=count"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            return res.json().get("D", {}).get("Pagination", {}).get("TotalRows")
        print(f"  [WARN] count HTTP {res.status_code}: {res.text[:200]}")
    except requests.RequestException as e:
        print(f"  [WARN] count request failed: {e}")
    return None


def spark_accessible_keys(mls_name: str, mls_id: str) -> tuple[set[str], int]:
    """
    Paginate Spark for every Active listingKey under this MLS.
    Returns (keys_set, pages_consumed).
    """
    filter_query = _spark_filter(mls_id)
    encoded_filter = requests.utils.quote(filter_query, safe=" '()=")

    expected = spark_total_count(filter_query)
    if expected is not None:
        print(f"  [{mls_name}] Spark reports {expected:,} Active keys")

    collected: set[str] = set()
    skiptoken = ""
    page = 0
    while True:
        page += 1
        url = (
            f"{BASE_URL}?_limit={PAGE_SIZE}"
            f"&_filter={encoded_filter}"
            f"&_select=ListingKey"
            f"&_skiptoken={skiptoken}"
        )
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"  [NET] page {page} failed: {e}")
            time.sleep(3)
            continue

        if res.status_code == 429:
            print(f"  [429] sleeping 10s")
            time.sleep(10)
            continue
        if res.status_code != 200:
            print(f"  [HTTP {res.status_code}] {res.text[:200]}")
            break

        body = res.json()
        results = body.get("D", {}).get("Results", [])
        for r in results:
            sf = r.get("StandardFields") or r
            k = sf.get("ListingKey")
            if k:
                collected.add(str(k))

        new_skiptoken = body.get("D", {}).get("SkipToken") or body.get("SkipToken")
        if not results or not new_skiptoken or new_skiptoken == skiptoken:
            break
        skiptoken = new_skiptoken

        # Gentle pacing
        time.sleep(0.2)

    return collected, page

# ──────────────────────────────────────────────────────────────────────────────
# 🧭 GUARD EVALUATION
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_guards(
    mls_name: str,
    db_active: int,
    spark_active: int,
    stale_count: int,
    args,
) -> list[str]:
    """Return a list of guard-failure reasons. Empty list == all guards pass."""
    reasons = []

    # Absolute Spark floor — catches the Apr 6 case directly
    if spark_active < args.min_spark_absolute:
        reasons.append(
            f"Spark returned only {spark_active:,} keys "
            f"(< floor {args.min_spark_absolute:,}). Likely a Spark glitch."
        )

    # Ratio guards only apply to MLSs with a meaningful active set
    if db_active >= SMALL_MLS_FLOOR:
        if spark_active < db_active * args.min_spark_ratio:
            reasons.append(
                f"Spark {spark_active:,} is < {int(args.min_spark_ratio * 100)}% of DB {db_active:,}. "
                "Spark appears to be under-reporting."
            )
        if stale_count > db_active * args.max_purge_ratio:
            pct = (stale_count / db_active * 100) if db_active else 0
            reasons.append(
                f"Would purge {stale_count:,} / {db_active:,} ({pct:.1f}%) — "
                f"exceeds cap {int(args.max_purge_ratio * 100)}%."
            )

    # Absolute cap applies to every MLS regardless of size
    if stale_count > args.max_purge_absolute:
        reasons.append(
            f"Would purge {stale_count:,} which exceeds absolute cap "
            f"{args.max_purge_absolute:,}."
        )

    return reasons

# ──────────────────────────────────────────────────────────────────────────────
# 💾 MONGO HELPERS
# ──────────────────────────────────────────────────────────────────────────────


def db_active_keys(collection, mls_name: str) -> set[str]:
    cursor = collection.find(
        {"mlsSource": mls_name, "standardStatus": "Active"},
        {"listingKey": 1},
    )
    return {str(doc["listingKey"]) for doc in cursor if doc.get("listingKey")}


def apply_purge(collection, stale_keys: list[str]) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    result = collection.update_many(
        {"listingKey": {"$in": stale_keys}},
        {"$set": {
            "standardStatus": "OffMarket",
            "statusLastChecked": now_iso,
            "purgedAt": now_iso,
        }},
    )
    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
        "purgedAt": now_iso,
    }

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Purge DB-active listings that Spark no longer considers Active. Safety-guarded."
    )
    parser.add_argument("--mls", choices=list(MLS_IDS.keys()),
                        help="Restrict to one MLS. Default: all 8.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to MongoDB. Without this, dry-run.")
    parser.add_argument("--min-spark-absolute", type=int, default=DEFAULT_MIN_SPARK_ABSOLUTE,
                        help=f"Abort an MLS if Spark returns fewer than this many keys (default {DEFAULT_MIN_SPARK_ABSOLUTE}).")
    parser.add_argument("--min-spark-ratio", type=float, default=DEFAULT_MIN_SPARK_RATIO,
                        help=f"Abort if Spark count < DB-active × this (default {DEFAULT_MIN_SPARK_RATIO}).")
    parser.add_argument("--max-purge-absolute", type=int, default=DEFAULT_MAX_PURGE_ABSOLUTE,
                        help=f"Abort if proposed purge > this many docs (default {DEFAULT_MAX_PURGE_ABSOLUTE}).")
    parser.add_argument("--max-purge-ratio", type=float, default=DEFAULT_MAX_PURGE_RATIO,
                        help=f"Abort if proposed purge > this %% of DB-active (default {DEFAULT_MAX_PURGE_RATIO}).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore guard failures and proceed. Not recommended. Logged as forced.")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("=" * 80)
    print(f"Purge-stale — mode: {mode}")
    print("=" * 80)
    print(f"Guards: min_spark_abs={args.min_spark_absolute}  min_spark_ratio={args.min_spark_ratio}  "
          f"max_purge_abs={args.max_purge_absolute}  max_purge_ratio={args.max_purge_ratio}  force={args.force}")

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client.get_database()
    collection = db.unified_listings

    target_mls = [args.mls] if args.mls else list(MLS_IDS.keys())

    run_summary = {
        "mode": "apply" if args.apply else "dryrun",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "guards": {
            "min_spark_absolute": args.min_spark_absolute,
            "min_spark_ratio": args.min_spark_ratio,
            "max_purge_absolute": args.max_purge_absolute,
            "max_purge_ratio": args.max_purge_ratio,
            "force": args.force,
        },
        "mls": {},
        "totals": {"purged": 0, "aborted_mls": 0, "processed_mls": 0},
    }

    for mls_name in target_mls:
        mls_id = MLS_IDS[mls_name]
        print(f"\n>>> {mls_name}")

        t0 = time.time()
        spark_keys, pages = spark_accessible_keys(mls_name, mls_id)
        print(f"  [{mls_name}] Spark accessible: {len(spark_keys):,} keys  (pages={pages}, {time.time()-t0:.1f}s)")

        active_keys = db_active_keys(collection, mls_name)
        print(f"  [{mls_name}] DB active:        {len(active_keys):,} keys")

        stale = sorted(active_keys - spark_keys)
        print(f"  [{mls_name}] Would purge:      {len(stale):,}")

        reasons = evaluate_guards(mls_name, len(active_keys), len(spark_keys), len(stale), args)

        mls_entry = {
            "spark_active": len(spark_keys),
            "db_active": len(active_keys),
            "stale_count": len(stale),
            "guard_failures": reasons,
            "purged": 0,
            "forced": False,
            "sample_stale_keys": stale[:25],
        }

        if reasons and not args.force:
            print(f"  [{mls_name}] ⛔ GUARDS TRIPPED — skipping purge:")
            for r in reasons:
                print(f"       - {r}")
            run_summary["totals"]["aborted_mls"] += 1
            run_summary["mls"][mls_name] = mls_entry
            continue

        if reasons and args.force:
            print(f"  [{mls_name}] ⚠️  Guards tripped but --force specified. Proceeding anyway.")
            for r in reasons:
                print(f"       - {r}")
            mls_entry["forced"] = True

        if not stale:
            print(f"  [{mls_name}] ✅ Nothing to purge.")
            run_summary["totals"]["processed_mls"] += 1
            run_summary["mls"][mls_name] = mls_entry
            continue

        if args.apply:
            res = apply_purge(collection, stale)
            mls_entry["purged"] = res["modified"]
            mls_entry["matched"] = res["matched"]
            mls_entry["purgedAt"] = res["purgedAt"]
            print(f"  [{mls_name}] ✅ Purged {res['modified']:,} docs (matched {res['matched']:,}).")
            run_summary["totals"]["purged"] += res["modified"]
        else:
            print(f"  [{mls_name}] (dry-run) would mark {len(stale):,} docs OffMarket with purgedAt.")

        run_summary["totals"]["processed_mls"] += 1
        run_summary["mls"][mls_name] = mls_entry

    run_summary["ended_utc"] = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"MLSs processed: {run_summary['totals']['processed_mls']}")
    print(f"MLSs aborted:   {run_summary['totals']['aborted_mls']}")
    print(f"Docs purged:    {run_summary['totals']['purged']:,}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = LOG_DIR / f"purge_{'apply' if args.apply else 'dryrun'}_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(run_summary, f, indent=2, default=str)
    print(f"  → {out_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
