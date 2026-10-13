#!/usr/bin/env python3
"""
Build Pool Premium

What a PRIVATE pool is worth in monthly rent, per subdivision and per ZIP,
measured from closed (rented) leases. Writes `poolPremium` onto
subdivisions.rentStats and onto the ZIP doc in rent_rates.

Kept as its own script rather than folded into build-rent-rates.py: the output
is purely additive, so there is no reason to edit a 605-line script that works,
and the nightly orchestrator benefits from the stage being explicit.

─── Why the method is fussy ───────────────────────────────────────────────────

1. `poolFeatures` arrives in TWO shapes across the 8 MLSs — a comma-separated
   string ("Private", "Association Pool", "Community, In Ground") AND a dict
   ({'none': True}, {'community': True, 'in Ground': True}). Both are handled.

2. A COMMUNITY or ASSOCIATION pool is not a private pool. Counting them together
   inflates the premium on every condo in a complex with a shared pool. They are
   excluded from both sides of the comparison, not lumped into "has pool".

3. ~11k of the sampled closed leases carry an explicit "none"/{'none': True}.
   The field being POPULATED says nothing about a pool existing — only its value
   does.

4. Pool presence correlates with detached-vs-attached and with square footage,
   so a naive with/without median split measures "is a house" as much as "has a
   pool". The comparison is therefore made WITHIN a (bedroom bucket,
   propertySubType) cell, and a rent-per-sqft delta is reported alongside the
   raw percentage as a cross-check.

Output `poolPremium` block:

{
  "lastUpdated": ISO, "scope": "subdivision"|"zip", "geo": {...},
  "method": "private-vs-none, matched on bedroom+subType, 24-month closed leases",
  "basis": "matched-cell-median" | "insufficient",
  "overallPctLift": float|None,          # sample-weighted across qualifying cells
  "sampleSize": int,                     # private + none leases in qualifying cells
  "byBed": [ { beds, bedsLabel, subType, privateCount, noneCount,
               medianRentPrivate, medianRentNone, pctLift, ppsfDelta } ],
  "quality": {"confidence": ..., "notes": [...]}
}

Usage:
    python3 src/scripts/cma/build-pool-premium.py --all
    python3 src/scripts/cma/build-pool-premium.py --zip 92211 --dry-run
    python3 src/scripts/cma/build-pool-premium.py --slug palm-desert-country-club --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

# ── constants ─────────────────────────────────────────────────────────────────
SAMPLE_WINDOW_MONTHS = 24
MIN_PER_SIDE = 5          # need this many private AND this many none in a cell
GOOD_CELLS = 2            # >= this many qualifying cells ⇒ "good"
MAX_BEDS_BUCKET = 5
BULK_BATCH = 200

LEASE_MATCH = {"$or": [{"propertyType": "B"}, {"propertyTypeName": "Residential Lease"}]}

PROJECTION = {
    "listingKey": 1, "city": 1, "postalCode": 1, "subdivisionName": 1,
    "bedsTotal": 1, "bedroomsTotal": 1, "propertySubType": 1, "livingArea": 1,
    "poolFeatures": 1, "closePrice": 1, "currentPrice": 1, "listPrice": 1,
}

# Tokens that mean a shared pool — never counted as private.
SHARED_TOKENS = ("community", "association")
# Tokens that mean no pool at all.
NONE_TOKENS = ("none",)
# Physical pool descriptors: a pool exists at the property.
PRIVATE_TOKENS = ("private", "in ground", "inground", "above ground", "heated",
                  "lap", "salt water", "saltwater", "filtered", "tile",
                  "gunite", "fenced", "waterfall", "infinity", "exercise",
                  "solar heat", "pebble")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=600000)
db = client.get_database()
subs_coll = db.subdivisions
closed_coll = db.unified_closed_listings
rent_rates_coll = db.rent_rates
print("✅ Connected to MongoDB")


def _escape_regex(s: str) -> str:
    return re.escape(s)


def pool_tokens(raw) -> list[str]:
    """Normalize poolFeatures (string OR dict) to a lowercase token list."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # {'community': True, 'in Ground': True} → only the True keys matter
        return [str(k).strip().lower() for k, v in raw.items() if v]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip().lower() for x in raw if x]
    return [t.strip().lower() for t in str(raw).split(",") if t.strip()]


def classify_pool(raw) -> str:
    """'private' | 'shared' | 'none' | 'unknown'.

    Order matters: an explicit 'none' wins, then a shared marker, and only then
    do physical descriptors imply a private pool. That ordering is what keeps
    {'association': True, 'in Ground': True} out of the private bucket.
    """
    toks = pool_tokens(raw)
    if not toks:
        return "unknown"
    if any(t in NONE_TOKENS or t.startswith("none") for t in toks):
        return "none"
    if any(any(s in t for s in SHARED_TOKENS) for t in toks):
        return "shared"
    if any(any(p in t for p in PRIVATE_TOKENS) for t in toks):
        return "private"
    return "unknown"


def beds_bucket(l: dict):
    b = l.get("bedsTotal")
    if b is None:
        b = l.get("bedroomsTotal")
    if not isinstance(b, (int, float)):
        return None
    b = int(b)
    return min(b, MAX_BEDS_BUCKET) if b >= 0 else None


def beds_label(b: int) -> str:
    return f"{b}+" if b >= MAX_BEDS_BUCKET else str(b)


def lease_rent(l: dict):
    for f in ("closePrice", "currentPrice", "listPrice"):
        v = l.get(f)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _ppsf(rent, sqft):
    if rent and isinstance(sqft, (int, float)) and sqft > 0:
        return round(rent / sqft, 2)
    return None


def assemble_pool_premium(scope: str, geo: dict, leases: list[dict]) -> dict | None:
    if not leases:
        return None
    now = datetime.now(timezone.utc)

    # cell = (bedroom bucket, propertySubType) → matched comparison inside it
    cells: dict = defaultdict(lambda: {"private": [], "none": []})
    seen_shared = seen_unknown = 0
    for l in leases:
        rent = lease_rent(l)
        b = beds_bucket(l)
        if rent is None or b is None:
            continue
        cls = classify_pool(l.get("poolFeatures"))
        if cls == "shared":
            seen_shared += 1
            continue
        if cls == "unknown":
            seen_unknown += 1
            continue
        sub_type = (l.get("propertySubType") or "Unknown").strip()
        cells[(b, sub_type)][cls].append(
            {"rent": rent, "ppsf": _ppsf(rent, l.get("livingArea"))})

    rows, weights, lifts, total = [], [], [], 0
    for (b, sub_type), sides in sorted(cells.items()):
        priv, non = sides["private"], sides["none"]
        if len(priv) < MIN_PER_SIDE or len(non) < MIN_PER_SIDE:
            continue
        mp = statistics.median([x["rent"] for x in priv])
        mn = statistics.median([x["rent"] for x in non])
        if mn <= 0:
            continue
        pct = round(mp / mn - 1, 4)
        pp = [x["ppsf"] for x in priv if x["ppsf"]]
        pn = [x["ppsf"] for x in non if x["ppsf"]]
        rows.append({
            "beds": b, "bedsLabel": beds_label(b), "subType": sub_type,
            "privateCount": len(priv), "noneCount": len(non),
            "medianRentPrivate": int(round(mp)), "medianRentNone": int(round(mn)),
            "pctLift": pct,
            "ppsfDelta": (round(statistics.median(pp) - statistics.median(pn), 2)
                          if pp and pn else None),
        })
        w = len(priv) + len(non)
        weights.append(w)
        lifts.append(pct)
        total += w

    notes: list[str] = []
    if seen_shared:
        notes.append(f"{seen_shared} community/association-pool leases excluded "
                     f"(a shared pool is not a private pool)")
    if seen_unknown:
        notes.append(f"{seen_unknown} leases had unreadable poolFeatures")

    base = {
        "lastUpdated": now.isoformat(),
        "scope": scope,
        "geo": geo,
        "method": "private-vs-none, matched on bedroom+subType, "
                  f"{SAMPLE_WINDOW_MONTHS}-month closed leases",
        "sampleSize": total,
    }
    if not rows:
        notes.append(f"no bedroom+subType cell had {MIN_PER_SIDE} private and "
                     f"{MIN_PER_SIDE} no-pool leases")
        base.update({"basis": "insufficient", "overallPctLift": None, "byBed": [],
                     "quality": {"confidence": "insufficient", "notes": notes}})
        return base

    overall = round(sum(l * w for l, w in zip(lifts, weights)) / sum(weights), 4)
    base.update({
        "basis": "matched-cell-median",
        "overallPctLift": overall,
        "byBed": rows,
        "quality": {"confidence": "good" if len(rows) >= GOOD_CELLS else "fair",
                    "notes": notes},
    })
    return base


def load_window(scope_match: dict | None) -> list[dict]:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=SAMPLE_WINDOW_MONTHS * 30)
    date_ceiling = now + timedelta(days=2)
    q = {"closeDate": {"$gte": window_start, "$lte": date_ceiling}, **LEASE_MATCH}
    if scope_match:
        q.update(scope_match)
    return list(closed_coll.find(q, PROJECTION))


def group_by_sub(items):
    g = defaultdict(list)
    for l in items:
        name = (l.get("subdivisionName") or "").strip().lower()
        city = (l.get("city") or "").strip().lower()
        if name and city:
            g[(city, name)].append(l)
    return g


def group_by_zip(items):
    g = defaultdict(list)
    for l in items:
        if l.get("postalCode"):
            g[l["postalCode"]].append(l)
    return g


def main():
    p = argparse.ArgumentParser(description="Build poolPremium on subdivisions + ZIPs.")
    p.add_argument("--all", action="store_true")
    p.add_argument("--subdivision", dest="subdivision_name")
    p.add_argument("--slug")
    p.add_argument("--city")
    p.add_argument("--county")
    p.add_argument("--zip", dest="postal")
    p.add_argument("--subs-only", action="store_true")
    p.add_argument("--zips-only", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not (args.slug or args.subdivision_name or args.city or args.county or args.postal or args.all):
        print("[ERROR] Must pass --all, --subdivision, --slug, --city, --county, or --zip.",
              file=sys.stderr)
        p.print_help()
        sys.exit(1)

    started = time.time()
    do_subs = not args.zips_only and not args.postal
    do_zips = not args.subs_only and not (args.subdivision_name or args.slug or args.county)

    scope_match = {"city": args.city} if args.city else None
    print("📥 Loading windowed closed leases…")
    leases = load_window(scope_match)
    dist = Counter(classify_pool(l.get("poolFeatures")) for l in leases)
    print(f"   {len(leases):,} closed leases in the {SAMPLE_WINDOW_MONTHS}-month window")
    print(f"   pool classes: {dict(dist)}")

    sub_groups, zip_groups = group_by_sub(leases), group_by_zip(leases)

    sub_written = sub_insufficient = sub_skipped = 0
    if do_subs:
        q: dict = {}
        if args.slug:
            q["slug"] = args.slug
        elif args.subdivision_name:
            q["name"] = {"$regex": f"^{_escape_regex(args.subdivision_name)}$", "$options": "i"}
        elif args.city:
            q["city"] = args.city
        elif args.county:
            q["county"] = {"$regex": f"^{_escape_regex(args.county)}$", "$options": "i"}
        cur = subs_coll.find(q)
        if args.limit:
            cur = cur.limit(args.limit)
        subs = list(cur)
        print(f"🏘️  {len(subs):,} subdivisions  (dry-run={args.dry_run})")
        pending: list[UpdateOne] = []
        for i, sub in enumerate(subs, 1):
            key = ((sub.get("city") or "").strip().lower(), (sub.get("name") or "").strip().lower())
            geo = {"subdivisionName": sub.get("name"), "slug": sub.get("slug"),
                   "city": sub.get("city"), "county": sub.get("county")}
            stats = assemble_pool_premium("subdivision", geo, sub_groups.get(key, []))
            if stats is None:
                sub_skipped += 1
                continue
            if stats["basis"] == "insufficient":
                sub_insufficient += 1
            elif args.verbose or True:
                print(f"  sub {i}/{len(subs)}: {sub.get('name','?')[:38]:<38} "
                      f"lift {stats['overallPctLift']:+.1%}  n={stats['sampleSize']}  "
                      f"[{stats['quality']['confidence']}]")
            if args.dry_run and i <= 2:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"_id": sub["_id"]},
                                         {"$set": {"rentStats.poolPremium": stats}}))
                if len(pending) >= BULK_BATCH:
                    sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count

    zip_written = zip_insufficient = 0
    if do_zips:
        keys = [args.postal] if args.postal else sorted(zip_groups, key=lambda z: -len(zip_groups[z]))
        if args.limit:
            keys = keys[:args.limit]
        print(f"🗺️  {len(keys):,} ZIPs  (dry-run={args.dry_run})")
        pending = []
        for i, z in enumerate(keys, 1):
            grp = zip_groups.get(z, [])
            cities = [l.get("city") for l in grp if l.get("city")]
            geo = {"postalCode": z, "city": Counter(cities).most_common(1)[0][0] if cities else None}
            stats = assemble_pool_premium("zip", geo, grp)
            if stats is None:
                continue
            if stats["basis"] == "insufficient":
                zip_insufficient += 1
            else:
                print(f"  zip {i}/{len(keys)}: {z:<8} {str(geo['city'])[:20]:<20} "
                      f"lift {stats['overallPctLift']:+.1%}  n={stats['sampleSize']:<5} "
                      f"cells={len(stats['byBed'])}  [{stats['quality']['confidence']}]")
            if args.dry_run and i <= 2:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"postalCode": z},
                                         {"$set": {"poolPremium": stats},
                                          "$setOnInsert": {"postalCode": z}}, upsert=True))
                if len(pending) >= BULK_BATCH:
                    rent_rates_coll.bulk_write(pending, ordered=False)
                    zip_written += len(pending)
                    pending.clear()
        if not args.dry_run and pending:
            rent_rates_coll.bulk_write(pending, ordered=False)
            zip_written += len(pending)

    el = time.time() - started
    print("\n" + "=" * 60)
    if do_subs:
        print(f"Subdivisions — written {sub_written:,}  insufficient {sub_insufficient:,}  skipped {sub_skipped:,}")
    if do_zips:
        print(f"ZIPs         — written {zip_written:,}  insufficient {zip_insufficient:,}")
    print(f"Elapsed:       {el/60:.1f} min" + ("  (dry-run, no writes)" if args.dry_run else ""))
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
