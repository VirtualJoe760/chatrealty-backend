#!/usr/bin/env python3
"""
Normalize unified_closed_listings.closeDate to a single BSON type.

One-off repair for the mixed-type field documented in
docs/db/vps-migration-plan.md §5. Two separate problems, two passes:

  1. STRINGS → DATES. The live status router wrote Spark's "YYYY-MM-DD"
     through untouched while historical/seed.py parsed it into a datetime, so
     the collection carried both. A {"closeDate": {"$gte": <datetime>}} range
     matches only the date-typed half, which was dropping most of the current
     year from every comp query. All string values are plain "YYYY-MM-DD".

  2. CORRUPT FUTURE DATES → QUARANTINE. Real values reach year 4008. They
     poison any most-recent-sale sort. The original value moves to
     `closeDateCorrupt` and `closeDate` is removed, so the row stops matching
     date ranges but stays inspectable. Nothing is deleted.

     The cutoff is seed.FUTURE_CLOSE_GRACE, not "today": seasonal leases are
     booked months ahead of occupancy, so a near-future closeDate is real data.

The producers are fixed too — seed.coerce_close_date now runs on every write —
so this script is not expected to find anything on a second run.

Usage:
    python src/scripts/utilities/normalize-close-dates.py              # dry run
    python src/scripts/utilities/normalize-close-dates.py --apply
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise SystemExit("[ERROR] MONGODB_URI is not set in .env.local")

# Reuse the exact rule the ingest writers now apply.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
from closedate import FUTURE_CLOSE_GRACE, coerce_close_date  # noqa: E402

COLLECTION = "unified_closed_listings"


def report(coll, ceiling):
    strings = coll.count_documents({"closeDate": {"$type": "string"}})
    dates = coll.count_documents({"closeDate": {"$type": "date"}})
    corrupt = coll.count_documents({"closeDate": {"$gt": ceiling}})
    missing = coll.count_documents({"closeDate": {"$exists": False}})
    print(f"  date-typed      {dates:>9,}")
    print(f"  string-typed    {strings:>9,}   <- pass 1")
    print(f"  beyond cutoff   {corrupt:>9,}   <- pass 2")
    print(f"  absent          {missing:>9,}")
    return strings, corrupt


def normalize_strings(coll, apply: bool) -> int:
    """Pass 1 — convert every string closeDate to a date, server-side."""
    q = {"closeDate": {"$type": "string"}}
    n = coll.count_documents(q)
    if not n:
        print("\n[pass 1] no string-typed closeDate values")
        return 0

    # Verify every value parses before touching anything: $convert would fail
    # the whole update on the first bad one.
    unparseable = [d["listingKey"] for d in coll.find(q, {"listingKey": 1, "closeDate": 1})
                   if coerce_close_date(d["closeDate"]) is None]
    if unparseable:
        print(f"\n[pass 1] ABORT: {len(unparseable)} value(s) will not parse, "
              f"first: {unparseable[:3]}")
        return 0

    print(f"\n[pass 1] {n:,} string values parse cleanly")
    if not apply:
        print("         (dry run — no write)")
        return 0

    res = coll.update_many(q, [{"$set": {"closeDate": {"$toDate": "$closeDate"}}}])
    print(f"         converted {res.modified_count:,}")
    return res.modified_count


def quarantine_corrupt(coll, ceiling, apply: bool) -> int:
    """Pass 2 — park implausible far-future dates in closeDateCorrupt."""
    q = {"closeDate": {"$gt": ceiling}}
    docs = list(coll.find(q, {"listingKey": 1, "closeDate": 1, "propertyType": 1}))
    if not docs:
        print("\n[pass 2] no closeDate beyond the cutoff")
        return 0

    print(f"\n[pass 2] {len(docs)} doc(s) beyond {ceiling:%Y-%m-%d}:")
    for d in sorted(docs, key=lambda x: x["closeDate"])[:5]:
        print(f"         {d.get('listingKey')}  {d['closeDate']:%Y-%m-%d}  type={d.get('propertyType')}")
    if len(docs) > 5:
        print(f"         … and {len(docs) - 5} more")

    if not apply:
        print("         (dry run — no write)")
        return 0

    ops = [UpdateOne({"_id": d["_id"]},
                     {"$set": {"closeDateCorrupt": d["closeDate"]},
                      "$unset": {"closeDate": ""}})
           for d in coll.find(q, {"closeDate": 1})]
    res = coll.bulk_write(ops, ordered=False)
    print(f"         quarantined {res.modified_count}")
    return res.modified_count


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="perform the writes (default is a dry run)")
    args = ap.parse_args()

    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=20000)
    coll = client["admin"][COLLECTION]
    ceiling = datetime.now(timezone.utc).replace(tzinfo=None) + FUTURE_CLOSE_GRACE

    print(f"{COLLECTION} — closeDate before:")
    report(coll, ceiling)

    normalize_strings(coll, args.apply)
    quarantine_corrupt(coll, ceiling, args.apply)

    if args.apply:
        print(f"\n{COLLECTION} — closeDate after:")
        report(coll, ceiling)
    else:
        print("\nNothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
