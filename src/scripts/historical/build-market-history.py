#!/usr/bin/env python3
"""
Build Market History (the annual archive that outlives the TTL)

`unified_closed_listings` carries a 5-year rolling TTL (`closeDate_ttl_5years`,
created in seed.py). That window is the feed's horizon, not a shortfall — Spark
only serves five years back — but it means every closing we hold today is on a
clock: most of 2021 has already aged out, and 2022 is next.

This script reads the closings while we still have them and writes one permanent
per-year aggregate per area into `market_history`, which has NO TTL. Five years
of detail becomes an indefinite series of annual medians. That series is what
makes a long-run appreciation estimate possible at all: without it, in 2030 we
would still only be able to see back to 2025.

It is cheap insurance, not an emergency. Run it once or twice a year; nothing
breaks if it is late, but anything it misses before the TTL fires is gone for
good.

── The one rule that matters ────────────────────────────────────────────────
A re-run must never make the archive worse. Once the TTL eats part of a year,
recomputing that year yields FEWER closings than the archived figure was built
from, and blindly upserting would overwrite a complete 2022 with a gutted one.
So a write that would lower `closedCount` for an existing doc is refused unless
--force. That guard is the reason this archive is worth keeping.

Scopes written: zip, city, subdivision. Property types: A (sales) and B (leases
— `closePrice` on a lease is the monthly rent, so the same aggregate doubles as
rent history for escalating forward).

Usage:
    # what the archive holds now, no writes
    python3 src/scripts/historical/build-market-history.py --status

    # one year, printed not written
    python3 src/scripts/historical/build-market-history.py --year 2024 --dry-run

    # every year the collection still has (the normal run)
    python3 src/scripts/historical/build-market-history.py --all

    # re-archive a year even if it now has fewer closings
    python3 src/scripts/historical/build-market-history.py --year 2022 --force
"""

import os
import sys
import time
import argparse
import statistics
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

from pymongo import MongoClient, UpdateOne, ASCENDING
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

# ── constants ─────────────────────────────────────────────────────────────────
PROPERTY_TYPES = ("A", "B")        # A = sale, B = lease (closePrice = monthly rent)
SCOPES = ("zip", "city", "subdivision")
MIN_SAMPLE = 5                     # below this an area-year is not worth storing
GOOD_SAMPLE = 30                   # at or above this the medians are dependable
FAIR_SAMPLE = 10
MIN_CLOSE_PRICE = 1000             # $1 token transactions and feed noise
MIN_SQFT = 200
BULK_BATCH = 500

ESTIMATE_NOTE = (
    "Annual medians over closed MLS transactions in this area. Estimates for "
    "trend use, not appraisals: the mix of what sold changes year to year, so a "
    "median move is not the same as one property's change in value."
)

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
closed_coll = db.unified_closed_listings
history_coll = db.market_history
print("✅ Connected to MongoDB")


def ensure_indexes():
    """`_id` is deterministic, so these only serve reads, not the upsert."""
    history_coll.create_index([("scope", ASCENDING), ("key", ASCENDING),
                               ("propertyType", ASCENDING), ("year", ASCENDING)],
                              name="area_year")
    history_coll.create_index([("year", ASCENDING)], name="year")


# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _as_date(v):
    """datetime or ISO string -> naive-comparable datetime, else None."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, str) and v.strip():
        t = v.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(t)
        except ValueError:
            return None
        return d.replace(tzinfo=None) if d.tzinfo else d
    return None


def days_on_market(doc):
    """Days from going on market to closing.

    `daysOnMarket` is absent from every closed doc in this feed — the field
    exists in the schema but Spark never populates it on a closed record — so it
    is derived from `onMarketDate`, which is present on effectively all of them.
    Falls back to `listingContractDate` when the on-market date is missing.
    """
    close = _as_date(doc.get("closeDate"))
    if close is None:
        return None
    stored = _num(doc.get("daysOnMarket"))
    if stored is not None:
        return stored
    start = _as_date(doc.get("onMarketDate")) or _as_date(doc.get("listingContractDate"))
    if start is None:
        return None
    return float((close - start).days)


def _med(vals, digits=2):
    return round(statistics.median(vals), digits) if vals else None


def _pct(vals, q, digits=2):
    """Percentile by position. Cheaper than a full quantiles() call and matches
    on small samples where quantiles() refuses to run."""
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[i], digits)


def confidence(n):
    if n >= GOOD_SAMPLE:
        return "good"
    if n >= FAIR_SAMPLE:
        return "fair"
    return "low"


def area_keys(doc):
    """Every (scope, key, label) this closing contributes to. A sale in a named
    subdivision counts toward its ZIP and city too — the scopes are overlapping
    views, not a partition.

    The label is only a display name, but ZIPs cross city lines, so it is
    tallied per area and the dominant one wins rather than whichever closing
    happened to be read first. The archive is permanent; an arbitrary label in
    it would be too."""
    out = []
    zp = (doc.get("postalCode") or "").strip()
    city = (doc.get("city") or "").strip()
    sub = (doc.get("subdivisionName") or "").strip()
    if zp:
        out.append(("zip", zp, city or None))
    if city:
        out.append(("city", city.lower(), city))
    if city and sub:
        # Subdivision names repeat across cities, so the city qualifies the key.
        out.append(("subdivision", f"{city.lower()}|{sub.lower()}", sub))
    return out


def doc_id(scope, key, ptype, year):
    return f"{scope}:{key}:{ptype}:{year}"


# ──────────────────────────────────────────────────────────────────────────────
# 📊 ONE YEAR
# ──────────────────────────────────────────────────────────────────────────────

def year_coverage(year):
    """Which months of this year the collection holds anything for.

    This is a property of the YEAR, not of any one area: the TTL truncates the
    oldest year and the current year is still running. A small subdivision with
    no sales in March is a thin area, not a truncated year, and conflating the
    two would mark most subdivision rows partial and invite a reader to discard
    them."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    rows = closed_coll.aggregate([
        {"$match": {"closeDate": {"$gte": start, "$lt": end},
                    "propertyType": {"$in": list(PROPERTY_TYPES)}}},
        {"$group": {"_id": {"$month": "$closeDate"}}},
    ], allowDiskUse=True)
    return sorted(r["_id"] for r in rows)


def collect_year(year, verbose=False):
    """Stream one calendar year and bucket its metrics by area and type.

    One pass per year rather than one over the whole collection: the peak memory
    is a single year's metric lists, and `closeDate_desc` makes the range scan
    cheap.
    """
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    q = {"closeDate": {"$gte": start, "$lt": end},
         "propertyType": {"$in": list(PROPERTY_TYPES)}}
    proj = {"_id": 0, "closePrice": 1, "listPrice": 1, "originalListPrice": 1,
            "livingArea": 1, "daysOnMarket": 1, "propertyType": 1,
            "postalCode": 1, "city": 1, "subdivisionName": 1, "closeDate": 1,
            "onMarketDate": 1, "listingContractDate": 1}

    # (scope, key, ptype) -> metric lists
    buckets = defaultdict(lambda: {"price": [], "ppsf": [], "dom": [], "ratio": [],
                                   "sqft": [], "months": set(), "labels": Counter()})
    seen = kept = 0
    for d in closed_coll.find(q, proj).batch_size(2000):
        seen += 1
        price = _num(d.get("closePrice"))
        if price is None or price < MIN_CLOSE_PRICE:
            continue
        kept += 1
        ptype = d.get("propertyType")
        sqft = _num(d.get("livingArea"))
        ppsf = price / sqft if sqft and sqft >= MIN_SQFT else None
        dom = days_on_market(d)
        ask = _num(d.get("originalListPrice")) or _num(d.get("listPrice"))
        ratio = price / ask if ask and ask >= MIN_CLOSE_PRICE else None
        month = d["closeDate"].month

        for scope, key, label in area_keys(d):
            b = buckets[(scope, key, ptype)]
            b["price"].append(price)
            b["months"].add(month)
            if label:
                b["labels"][label] += 1
            if ppsf is not None:
                b["ppsf"].append(ppsf)
            if sqft:
                b["sqft"].append(sqft)
            if dom is not None and 0 <= dom <= 1095:
                b["dom"].append(dom)
            if ratio is not None and 0.5 <= ratio <= 1.5:
                b["ratio"].append(ratio)

    if verbose:
        print(f"  {year}: scanned {seen:,} closings, kept {kept:,}, "
              f"{len(buckets):,} area-type buckets")
    return buckets


def build_docs(year, buckets, now, year_months):
    """Turn the buckets into storable docs. Every figure a reader needs is
    computed here and stored; the read surface never recomputes one."""
    docs = []
    # One verdict for the whole year, applied to every area in it.
    year_partial = len(year_months) < 12 or year >= now.year
    for (scope, key, ptype), b in buckets.items():
        n = len(b["price"])
        if n < MIN_SAMPLE:
            continue
        months = len(b["months"])
        docs.append({
            "_id": doc_id(scope, key, ptype, year),
            "scope": scope,
            "key": key,
            "label": b["labels"].most_common(1)[0][0] if b["labels"] else None,
            "propertyType": ptype,
            "year": year,
            "closedCount": n,
            "medianClosePrice": _med(b["price"]),
            "meanClosePrice": round(statistics.fmean(b["price"]), 2),
            "p25ClosePrice": _pct(b["price"], 0.25),
            "p75ClosePrice": _pct(b["price"], 0.75),
            "medianPricePerSqft": _med(b["ppsf"]),
            "medianLivingArea": _med(b["sqft"], 0),
            "medianDaysOnMarket": _med(b["dom"], 0),
            "medianCloseToListRatio": _med(b["ratio"], 4),
            "sampleSizes": {"price": n, "pricePerSqft": len(b["ppsf"]),
                            "daysOnMarket": len(b["dom"]),
                            "closeToListRatio": len(b["ratio"])},
            # Months this AREA had a closing in — a thin-area signal. A small
            # subdivision with 7 months is normal, not a gap in the data.
            "monthsCovered": months,
            # Whether the YEAR itself is truncated or still running. A partial
            # year must never be read as a full one: in a market this seasonal
            # the mix of months alone moves a median.
            "partialYear": year_partial,
            "yearMonthsCovered": len(year_months),
            "quality": {"confidence": confidence(n), "basis": "closed MLS transactions"},
            "source": "unified_closed_listings",
            "archivedAt": now,
            "note": ESTIMATE_NOTE,
        })
    return docs


def write_docs(docs, force, dry_run):
    """Upsert, refusing any write that would shrink an existing archive."""
    if not docs:
        return 0, 0, 0
    existing = {d["_id"]: d.get("closedCount", 0) for d in history_coll.find(
        {"_id": {"$in": [d["_id"] for d in docs]}}, {"closedCount": 1})}

    pending, protected = [], []
    for d in docs:
        prev = existing.get(d["_id"])
        if prev is not None and d["closedCount"] < prev and not force:
            protected.append((d["_id"], prev, d["closedCount"]))
            continue
        pending.append(UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True))

    if protected:
        print(f"  🛡️  kept {len(protected):,} archived doc(s) that the live "
              f"collection can no longer reproduce (the TTL has moved past "
              f"them). Examples:")
        for _id, prev, now_n in protected[:5]:
            print(f"        {_id}  archived {prev:,} vs recomputed {now_n:,}")
        print("        Pass --force only if you mean to replace them.")

    if dry_run:
        return 0, len(pending), len(protected)

    written = 0
    for i in range(0, len(pending), BULK_BATCH):
        res = history_coll.bulk_write(pending[i:i + BULK_BATCH], ordered=False)
        written += res.upserted_count + res.modified_count
    return written, len(pending), len(protected)


# ──────────────────────────────────────────────────────────────────────────────
# 📋 STATUS
# ──────────────────────────────────────────────────────────────────────────────

def available_years():
    """Years the live collection can still produce, oldest first.

    Counted over the SAME property types the archive stores, so --status is a
    like-for-like comparison. Types C and D (multi-family/commercial, land) are
    deliberately out of scope here."""
    rows = list(closed_coll.aggregate([
        {"$match": {"closeDate": {"$type": "date"},
                    "propertyType": {"$in": list(PROPERTY_TYPES)}}},
        {"$group": {"_id": {"$year": "$closeDate"}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ], allowDiskUse=True))
    return [(r["_id"], r["n"]) for r in rows if 2000 <= r["_id"] <= 2100]


def status():
    print("\nlive collection (what the TTL still holds, types "
          f"{'+'.join(PROPERTY_TYPES)}):")
    for year, n in available_years():
        months = len(year_coverage(year))
        flag = "" if months == 12 else f"   ({months} of 12 months)"
        print(f"  {year}  {n:>9,} closings{flag}")
    print("\narchive (market_history):")
    rows = list(history_coll.aggregate([
        {"$group": {"_id": {"y": "$year", "s": "$scope"},
                    "areas": {"$sum": 1},
                    "closings": {"$sum": "$closedCount"},
                    "partial": {"$sum": {"$cond": ["$partialYear", 1, 0]}}}},
        {"$sort": {"_id.y": 1, "_id.s": 1}},
    ]))
    if not rows:
        print("  empty — nothing archived yet")
        return
    for r in rows:
        flag = f"  ({r['partial']:,} partial)" if r["partial"] else ""
        print(f"  {r['_id']['y']}  {r['_id']['s']:<12} {r['areas']:>6,} areas"
              f"  {r['closings']:>9,} closings{flag}")
    print(f"\n  total docs: {history_coll.count_documents({}):,}")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, help="archive one calendar year")
    ap.add_argument("--all", action="store_true",
                    help="archive every year the collection still holds")
    ap.add_argument("--status", action="store_true",
                    help="compare the live collection against the archive, no writes")
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write")
    ap.add_argument("--force", action="store_true",
                    help="allow a write that lowers an archived closedCount")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return

    if not args.year and not args.all:
        ap.error("pass --year YYYY, --all, or --status")

    ensure_indexes()
    now = datetime.now(timezone.utc)

    if args.year:
        years = [args.year]
    else:
        years = [y for y, _ in available_years()]
        print(f"\nyears available: {', '.join(str(y) for y in years)}")

    started = time.time()
    tot_docs = tot_written = tot_protected = 0
    for year in years:
        print(f"\n── {year} ──")
        year_months = year_coverage(year)
        if len(year_months) < 12:
            print(f"  ⚠️  the collection holds only {len(year_months)} month(s) "
                  f"of {year} — every row is flagged partialYear")
        buckets = collect_year(year, verbose=True)
        docs = build_docs(year, buckets, now, year_months)
        by_scope = defaultdict(int)
        for d in docs:
            by_scope[d["scope"]] += 1
        print(f"  {len(docs):,} area-year docs  "
              + "  ".join(f"{s}={by_scope[s]:,}" for s in SCOPES))
        if args.verbose or args.dry_run:
            for d in sorted(docs, key=lambda x: -x["closedCount"])[:5]:
                print(f"    {d['_id']:<40} n={d['closedCount']:>6,} "
                      f"median ${d['medianClosePrice']:>12,.0f}  "
                      f"ppsf {d['medianPricePerSqft']}  "
                      f"{'PARTIAL' if d['partialYear'] else 'full'}")
        written, attempted, protected = write_docs(docs, args.force, args.dry_run)
        tot_docs += len(docs)
        tot_written += written
        tot_protected += protected
        print(f"  {'would write' if args.dry_run else 'written'}: "
              f"{attempted if args.dry_run else written:,}")
        buckets.clear()

    print("\n" + "=" * 60)
    print(f"Area-year docs built: {tot_docs:,}")
    print(f"Written:              {tot_written:,}" if not args.dry_run
          else "Dry-run (no writes)")
    if tot_protected:
        print(f"Protected from shrink:{tot_protected:,}")
    print(f"Elapsed:              {(time.time()-started)/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
