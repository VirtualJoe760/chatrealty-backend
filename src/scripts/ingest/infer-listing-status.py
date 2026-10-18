#!/usr/bin/env python3
"""
Infer Listing Status  (the expired / likely-closed call)

update-status.py already DETECTS a listing that vanished from the Spark feed:
MISS_ESCALATION_THRESHOLD = 2 means two consecutive misses move the doc into
unified_off_market stamped `movedToOffMarketAt`. What it never did was make the
final call — the doc keeps its LAST KNOWN status, which is why
unified_off_market holds ~71k documents still labelled "Active" and exactly one
labelled "Expired".

This pass makes that call, and writes it to NEW fields only:

    inferredStatus    "expired" | "likely-closed" | "unknown" | <feed status>
    inferredAt        ISO timestamp of this run
    inferenceBasis    which rule fired, so the answer is explainable
    daysOffMarket     int, the age the rule used
    inferenceSource   which timestamp field the age came from

`standardStatus` is NEVER modified. A guess must not overwrite a fact, and
leaving the MLS value intact is what lets a reappearing listing self-heal —
update-status.py routes it back out of off_market on its own.

── The rules ──────────────────────────────────────────────────────────────────
1. A terminal status from the FEED always wins. Expired / Canceled / Cancelled /
   Withdrawn / Closed are facts, not inferences; they pass straight through.
2. Gone < GRACE_DAYS  -> "unknown". The grace window. A listing can vanish for a
   few days because of a feed hiccup, or sit quietly under contract. Calling it
   expired here is the expensive mistake.
3. Gone >= GRACE_DAYS, last seen Active/ComingSoon -> "expired". It left the
   market without ever going under contract.
4. Gone >= GRACE_DAYS, last seen Pending/Active Under Contract -> "likely-closed",
   NOT expired. A listing in escrow that disappears has almost always closed on
   the ~30-day cycle. These are flagged for a closed-sync lookup instead of
   being called dead, because mislabelling a sale as a failure is the error that
   actually costs the agent.
5. Anything else (Hold, OffMarket, unreadable) -> "unknown" with its reason.

Timestamp fallback: movedToOffMarketAt -> offMarketDate -> modificationTimestamp.
Coverage of the first field is not guaranteed across all 8 MLSs, and a doc with
no usable timestamp is left "unknown" rather than aged from nothing.

Usage:
    python3 src/scripts/mls/backend/unified/infer-listing-status.py --dry-run
    python3 src/scripts/mls/backend/unified/infer-listing-status.py --all
    python3 src/scripts/mls/backend/unified/infer-listing-status.py --all --grace-days 45
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

GRACE_DAYS = 30          # a transaction normally closes in ~30 days
BULK_BATCH = 1000

# Statuses the feed states outright — never inferred over.
TERMINAL = {"Expired", "Canceled", "Cancelled", "Withdrawn", "Closed"}
# Last-known states that mean "was on the open market".
ON_MARKET = {"Active", "ComingSoon", "Coming Soon"}
# Last-known states that mean "a transaction was in progress".
IN_ESCROW = {"Pending", "Active Under Contract"}

TS_FIELDS = ("movedToOffMarketAt", "offMarketDate", "modificationTimestamp")

# ── Migration-seed detection ──────────────────────────────────────────────────
# unified_off_market was bulk-seeded on 2026-04-23: 28,483 docs carry a
# movedToOffMarketAt of 2026-04-23T07:00:2x, incrementing sub-second, written by
# one loop calling now() per doc. That stamp records WHEN THE COLLECTION WAS
# BUILT, not when the listing left the feed. Read at face value it reports a
# precise 173-day age for listings that went quiet years earlier — the batch
# contains 2019-keyed rows.
#
# 90.8% of that cohort is last-status OffMarket and already resolves to
# "unknown", so the real exposure is the ~2,610 Active/Pending/AUC docs. For
# those the age is a FLOOR, not a measurement, and `ageIsFloor` says so.
# modificationTimestamp is preferred when it predates the seed, because a real
# MLS touch is better evidence than the migration date. Only an EARLIER stamp is
# accepted, so an age can grow but never shrink below the grace window and flip
# a verdict.
SEED_DAY = date(2026, 4, 23)

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=600000)
db = client.get_database()
om_coll = db.unified_off_market
print("✅ Connected to MongoDB")


def coerce_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v.strip():
        s = v.strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def pick_timestamp(doc: dict):
    """First usable timestamp, which field it came from, and whether the age it
    yields is only a floor (migration-seeded rather than observed)."""
    for f in TS_FIELDS:
        d = coerce_dt(doc.get(f))
        if not d:
            continue
        if f == "movedToOffMarketAt" and d.date() == SEED_DAY:
            mod = coerce_dt(doc.get("modificationTimestamp"))
            if mod and mod < d:
                return mod, "modificationTimestamp-pre-seed", True
            return d, "migration-seed-floor", True
        return d, f, False
    return None, None, False


def infer(doc: dict, now: datetime, grace_days: int) -> dict:
    status = (doc.get("standardStatus") or "").strip()

    if status in TERMINAL:
        return {"inferredStatus": status, "inferenceBasis": "feed-terminal-status",
                "daysOffMarket": None, "inferenceSource": "standardStatus",
                "ageIsFloor": False}

    ts, src, floor = pick_timestamp(doc)
    if ts is None:
        return {"inferredStatus": "unknown",
                "inferenceBasis": "no-usable-timestamp",
                "daysOffMarket": None, "inferenceSource": None,
                "ageIsFloor": False}

    days = (now - ts).days
    if days < 0:                      # future-dated junk (this data has year-5015 rows)
        return {"inferredStatus": "unknown", "inferenceBasis": "future-dated-timestamp",
                "daysOffMarket": days, "inferenceSource": src, "ageIsFloor": floor}

    if days < grace_days:
        return {"inferredStatus": "unknown",
                "inferenceBasis": f"grace-window-under-{grace_days}d",
                "daysOffMarket": days, "inferenceSource": src, "ageIsFloor": floor}

    if status in ON_MARKET:
        return {"inferredStatus": "expired",
                "inferenceBasis": f"gone-{grace_days}d-plus-from-{status.lower()}",
                "daysOffMarket": days, "inferenceSource": src, "ageIsFloor": floor}

    if status in IN_ESCROW:
        return {"inferredStatus": "likely-closed",
                "inferenceBasis": f"gone-{grace_days}d-plus-from-escrow-({status})",
                "daysOffMarket": days, "inferenceSource": src, "ageIsFloor": floor,
                "needsClosedLookup": True}

    return {"inferredStatus": "unknown",
            "inferenceBasis": f"unhandled-last-status-({status or 'empty'})",
            "daysOffMarket": days, "inferenceSource": src, "ageIsFloor": floor}


def main():
    p = argparse.ArgumentParser(description="Infer expired / likely-closed on unified_off_market.")
    p.add_argument("--all", action="store_true", help="Process every off-market doc.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    p.add_argument("--dry-run", action="store_true", help="Compute and summarize, no writes.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not (args.all or args.limit or args.dry_run):
        print("[ERROR] Pass --all (or --limit / --dry-run).", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    started = time.time()
    now = datetime.now(timezone.utc)
    proj = {"listingKey": 1, "standardStatus": 1, "city": 1,
            "movedToOffMarketAt": 1, "offMarketDate": 1, "modificationTimestamp": 1}
    cursor = om_coll.find({}, proj, batch_size=2000)
    if args.limit:
        cursor = cursor.limit(args.limit)

    status_counts: Counter = Counter()
    basis_counts: Counter = Counter()
    src_counts: Counter = Counter()
    pending: list[UpdateOne] = []
    processed = written = 0
    samples: list[dict] = []

    for doc in cursor:
        processed += 1
        res = infer(doc, now, args.grace_days)
        status_counts[res["inferredStatus"]] += 1
        basis_counts[res["inferenceBasis"]] += 1
        src_counts[res["inferenceSource"]] += 1
        if len(samples) < 8 and res["inferredStatus"] in ("expired", "likely-closed"):
            samples.append({"listingKey": doc.get("listingKey"), "city": doc.get("city"),
                            "lastStatus": doc.get("standardStatus"), **res})
        if not args.dry_run:
            pending.append(UpdateOne({"_id": doc["_id"]},
                                     {"$set": {**res, "inferredAt": now.isoformat()}}))
            if len(pending) >= BULK_BATCH:
                written += om_coll.bulk_write(pending, ordered=False).modified_count
                pending.clear()
                if args.verbose:
                    print(f"   …{processed:,} processed")
    if not args.dry_run and pending:
        written += om_coll.bulk_write(pending, ordered=False).modified_count

    el = time.time() - started
    print("\n" + "=" * 64)
    print(f"Processed:     {processed:,}")
    print(f"Written:       {written:,}" + ("  (dry-run, no writes)" if args.dry_run else ""))
    print("\ninferredStatus:")
    for k, v in status_counts.most_common():
        print(f"   {str(k):<18}{v:>8,}")
    print("\ninferenceBasis:")
    for k, v in basis_counts.most_common(10):
        print(f"   {str(k):<46}{v:>8,}")
    print("\ntimestamp source used:")
    for k, v in src_counts.most_common():
        print(f"   {str(k):<26}{v:>8,}")
    if samples:
        print("\nsamples:")
        for s in samples:
            print(f"   {str(s.get('listingKey'))[:26]:<26} {str(s.get('city'))[:16]:<16} "
                  f"{str(s.get('lastStatus')):<22} -> {s['inferredStatus']:<14} "
                  f"({s['daysOffMarket']}d via {s['inferenceSource']})")
    print(f"\nElapsed: {el/60:.1f} min")
    print("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
