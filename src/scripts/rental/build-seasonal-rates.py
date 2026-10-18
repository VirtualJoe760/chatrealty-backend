#!/usr/bin/env python3
"""
Build Seasonal Rates  (layer 1.5 aggregation)

Rolls the per-listing `rateProfile` blocks written by build-rate-profiles.py into
area-level seasonal going rates, per subdivision and per ZIP, **split by bedroom
count** — the shape a cash-flow answer needs ("what does a 3/2 get in season").

Why this exists: `rateProfile` is per-listing and cash-flow matches on area, so
nothing bridged the two. build-rate-profiles.py has been writing rateProfile to
~15.8k active leases twice a week and no script read the field.

Output `seasonal` block (written to subdivisions.rentStats.seasonal and onto the
ZIP doc in rent_rates as `seasonal`):

{
  "lastUpdated": ISO, "scope": "subdivision"|"zip", "geo": {...},
  "basis": "tiered-median" | "insufficient",
  "sampleSize":     int,            # listings with a TIERED profile (real seasonality)
  "flatSampleSize": int,            # listings with a FLAT profile (no seasonality)
  "peakRate": int|None,             # median of per-listing peakRate
  "offPeakRate": int|None,          # median of per-listing offPeakRate
  "annualizedMonthly": int|None,    # median of per-listing annualizedMonthly
  "peakMonths": [int],              # most common peak-month set
  "byBed": [ {beds,bedsLabel,count,peakRate,offPeakRate,annualizedMonthly} ],
  "quality": {"confidence": "good"|"fair"|"insufficient", "notes": [str]}
}

IMPORTANT — unit discipline: `annualizedMonthly` (sum(rate x months) / 12) is the
ONLY field downstream cash-flow math may consume. peakRate/offPeakRate are a
display range; feeding a raw peak rate into a payment calculation overstates
income by 2x or more.

Seasonal signal is scarce — only ~4.75% of rateProfiles come back "tiered" — so a
single subdivision is often 0-2 comps. This script never extrapolates: below
INSUFFICIENT_TIERED it writes basis "insufficient" and leaves the rates None. The
subdivision -> ZIP fallback is the rollup's job (build-cashflow-profile.py), which
records which source it used.

Usage:
    python3 src/scripts/cma/build-seasonal-rates.py --all
    python3 src/scripts/cma/build-seasonal-rates.py --slug palm-desert-country-club --dry-run
    python3 src/scripts/cma/build-seasonal-rates.py --city "Palm Desert" --verbose
    python3 src/scripts/cma/build-seasonal-rates.py --zip 92211 --dry-run
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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

# ── constants ─────────────────────────────────────────────────────────────────
INSUFFICIENT_TIERED = 3       # < this many tiered profiles ⇒ insufficient, rates stay None
GOOD_TIERED = 8               # >= this many ⇒ "good" confidence
MAX_BEDS_BUCKET = 5           # 5 means "5+", matching build-rent-rates.py
BULK_BATCH = 200

LEASE_MATCH = {"$or": [{"propertyType": "B"}, {"propertyTypeName": "Residential Lease"}]}

PROFILE_PROJECTION = {
    "listingKey": 1, "city": 1, "postalCode": 1, "subdivisionName": 1,
    "bedsTotal": 1, "bedroomsTotal": 1, "rateProfile": 1,
}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=600000)
db = client.get_database()
subs_coll = db.subdivisions
listings_coll = db.unified_listings
rent_rates_coll = db.rent_rates
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS  (shared idioms with build-rent-rates.py)
# ──────────────────────────────────────────────────────────────────────────────


def _escape_regex(s: str) -> str:
    return re.escape(s)


def _median_int(values) -> int | None:
    vals = [v for v in values if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    return int(round(statistics.median(vals)))


def beds_bucket(listing: dict):
    """Normalize bed count into a 0..5 bucket where 5 means '5+'."""
    b = listing.get("bedsTotal")
    if b is None:
        b = listing.get("bedroomsTotal")
    if not isinstance(b, (int, float)):
        return None
    b = int(b)
    if b < 0:
        return None
    return min(b, MAX_BEDS_BUCKET)


def beds_label(b: int) -> str:
    return f"{b}+" if b >= MAX_BEDS_BUCKET else str(b)


def peak_months(profile: dict) -> list[int]:
    """Months belonging to the highest-rate tier."""
    tiers = profile.get("tiers") or []
    best, months = None, []
    for t in tiers:
        rate = t.get("monthlyRate")
        if isinstance(rate, (int, float)) and (best is None or rate > best):
            best, months = rate, [m for m in (t.get("months") or []) if isinstance(m, int)]
    return sorted(months)


def split_profiles(leases: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition into (tiered, flat). Only tiered carries real seasonality."""
    tiered, flat = [], []
    for l in leases:
        rp = l.get("rateProfile") or {}
        t = rp.get("type")
        if t == "tiered" and rp.get("peakRate") and rp.get("offPeakRate"):
            tiered.append(l)
        elif t == "flat":
            flat.append(l)
    return tiered, flat


def by_bed_seasonal(tiered: list[dict]) -> list[dict]:
    """Per-bedroom seasonal medians. Buckets below 2 comps are reported with their
    count so a consumer can discount them rather than being silently dropped."""
    g = defaultdict(list)
    for l in tiered:
        b = beds_bucket(l)
        if b is not None:
            g[b].append(l["rateProfile"])
    out = []
    for b in sorted(g):
        profs = g[b]
        out.append({
            "beds": b,
            "bedsLabel": beds_label(b),
            "count": len(profs),
            "peakRate": _median_int(p.get("peakRate") for p in profs),
            "offPeakRate": _median_int(p.get("offPeakRate") for p in profs),
            "annualizedMonthly": _median_int(p.get("annualizedMonthly") for p in profs),
        })
    return out


def build_quality(n_tiered: int, n_flat: int) -> dict:
    notes: list[str] = []
    if n_tiered >= GOOD_TIERED:
        conf = "good"
    elif n_tiered >= INSUFFICIENT_TIERED:
        conf = "fair"
        notes.append(f"only {n_tiered} tiered lease comps — treat seasonal rates as indicative")
    else:
        conf = "insufficient"
        notes.append(f"{n_tiered} tiered lease comps, need {INSUFFICIENT_TIERED}")
    if n_flat:
        notes.append(f"{n_flat} flat-rate leases excluded from seasonal medians")
    return {"confidence": conf, "notes": notes}


# ──────────────────────────────────────────────────────────────────────────────
# 🧱 ASSEMBLY
# ──────────────────────────────────────────────────────────────────────────────


def assemble_seasonal(scope: str, geo: dict, leases: list[dict]) -> dict | None:
    if not leases:
        return None

    tiered, flat = split_profiles(leases)
    quality = build_quality(len(tiered), len(flat))
    base = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "geo": geo,
        "sampleSize": len(tiered),
        "flatSampleSize": len(flat),
        "quality": quality,
    }

    if quality["confidence"] == "insufficient":
        base.update({
            "basis": "insufficient",
            "peakRate": None, "offPeakRate": None, "annualizedMonthly": None,
            "peakMonths": [], "byBed": [],
        })
        return base

    profs = [l["rateProfile"] for l in tiered]
    month_sets = Counter(tuple(peak_months(p)) for p in profs if peak_months(p))
    base.update({
        "basis": "tiered-median",
        "peakRate": _median_int(p.get("peakRate") for p in profs),
        "offPeakRate": _median_int(p.get("offPeakRate") for p in profs),
        "annualizedMonthly": _median_int(p.get("annualizedMonthly") for p in profs),
        "peakMonths": list(month_sets.most_common(1)[0][0]) if month_sets else [],
        "byBed": by_bed_seasonal(tiered),
    })
    return base


# ──────────────────────────────────────────────────────────────────────────────
# 📥 LOAD & GROUP
# ──────────────────────────────────────────────────────────────────────────────


def load_profiled_leases(scope_match: dict | None) -> list[dict]:
    """Active leases that already carry a rateProfile. One scan, grouped in memory —
    same reason build-rent-rates.py does it this way."""
    q = {"standardStatus": {"$in": ["Active", "ComingSoon"]},
         "rateProfile": {"$exists": True}, **LEASE_MATCH}
    if scope_match:
        q.update(scope_match)
    return list(listings_coll.find(q, PROFILE_PROJECTION))


def group_by_sub(items: list[dict]) -> dict:
    g = defaultdict(list)
    for l in items:
        name = (l.get("subdivisionName") or "").strip().lower()
        city = (l.get("city") or "").strip().lower()
        if name and city:
            g[(city, name)].append(l)
    return g


def group_by_zip(items: list[dict]) -> dict:
    g = defaultdict(list)
    for l in items:
        z = l.get("postalCode")
        if z:
            g[z].append(l)
    return g


def dominant_postal_for_subs(scope_match: dict | None) -> dict:
    """Map (city, subdivisionName) -> most common postalCode across ALL its listings
    (sale + lease). The rollup needs this to fall back from a thin subdivision to
    its ZIP; subdivisions carry no ZIP field of their own."""
    q = {"postalCode": {"$exists": True, "$ne": None},
         "subdivisionName": {"$exists": True, "$nin": [None, ""]}}
    if scope_match:
        q.update(scope_match)
    counts: dict = defaultdict(Counter)
    for l in listings_coll.find(q, {"city": 1, "subdivisionName": 1, "postalCode": 1}):
        name = (l.get("subdivisionName") or "").strip().lower()
        city = (l.get("city") or "").strip().lower()
        if name and city:
            counts[(city, name)][l["postalCode"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in counts.items() if v}


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Aggregate rateProfile into seasonal going rates.")
    parser.add_argument("--all", action="store_true", help="Process everything.")
    parser.add_argument("--subdivision", dest="subdivision_name", help="One subdivision by display name.")
    parser.add_argument("--slug", help="One subdivision by slug.")
    parser.add_argument("--city", help="All subdivisions + ZIPs in this city.")
    parser.add_argument("--county", help="All subdivisions in this county.")
    parser.add_argument("--zip", dest="postal", help="One ZIP.")
    parser.add_argument("--subs-only", action="store_true", help="Subdivisions only.")
    parser.add_argument("--zips-only", action="store_true", help="ZIPs only.")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write.")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-item logging.")
    args = parser.parse_args()

    if not (args.slug or args.subdivision_name or args.city or args.county or args.postal or args.all):
        print("[ERROR] Must pass --all, --subdivision, --slug, --city, --county, or --zip.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    started = time.time()
    do_subs = not args.zips_only and not args.postal
    do_zips = not args.subs_only and not (args.subdivision_name or args.slug or args.county)

    scope_match = {"city": args.city} if args.city else None
    print("📥 Loading active leases that carry a rateProfile…")
    leases = load_profiled_leases(scope_match)
    t_all, f_all = split_profiles(leases)
    print(f"   {len(leases):,} profiled leases — {len(t_all):,} tiered / {len(f_all):,} flat")

    sub_groups = group_by_sub(leases)
    zip_groups = group_by_zip(leases)

    # ── Subdivisions ──────────────────────────────────────────────────────────
    sub_written = sub_skipped = sub_insufficient = sub_failed = 0
    if do_subs:
        print("🗺️  Deriving dominant postalCode per subdivision…")
        dom_zip = dominant_postal_for_subs(scope_match)
        print(f"   resolved for {len(dom_zip):,} subdivisions")

        sub_query: dict = {}
        if args.slug:
            sub_query["slug"] = args.slug
        elif args.subdivision_name:
            sub_query["name"] = {"$regex": f"^{_escape_regex(args.subdivision_name)}$", "$options": "i"}
        elif args.city:
            sub_query["city"] = args.city
        elif args.county:
            sub_query["county"] = {"$regex": f"^{_escape_regex(args.county)}$", "$options": "i"}
        cursor = subs_coll.find(sub_query)
        if args.limit:
            cursor = cursor.limit(args.limit)
        subs = list(cursor)
        print(f"🏘️  {len(subs):,} subdivisions to process  (dry-run={args.dry_run})")

        pending: list[UpdateOne] = []
        for i, sub in enumerate(subs, 1):
            label = f"{sub.get('city','?')} / {sub.get('name','?')}"
            key = ((sub.get("city") or "").strip().lower(), (sub.get("name") or "").strip().lower())
            geo = {"subdivisionName": sub.get("name"), "slug": sub.get("slug"),
                   "city": sub.get("city"), "county": sub.get("county")}
            try:
                stats = assemble_seasonal("subdivision", geo, sub_groups.get(key, []))
            except Exception as e:
                sub_failed += 1
                print(f"  [ERR] {label}: {e}")
                continue

            set_doc: dict = {}
            if key in dom_zip:
                set_doc["dominantPostalCode"] = dom_zip[key]
            if stats is not None:
                set_doc["rentStats.seasonal"] = stats
                conf = stats["quality"]["confidence"]
                if conf == "insufficient":
                    sub_insufficient += 1
                elif args.verbose or conf != "insufficient":
                    print(f"Processing sub {i}/{len(subs)}: {label[:40]:<40} "
                          f"{stats['sampleSize']:>3} tiered  peak ${stats['peakRate'] or 0:,}  "
                          f"off ${stats['offPeakRate'] or 0:,}  ann ${stats['annualizedMonthly'] or 0:,}  [{conf}]")
            else:
                sub_skipped += 1

            if args.dry_run and stats is not None and i <= 2:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run and set_doc:
                pending.append(UpdateOne({"_id": sub["_id"]}, {"$set": set_doc}))
                if len(pending) >= BULK_BATCH:
                    sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count

    # ── ZIPs ──────────────────────────────────────────────────────────────────
    zip_written = zip_skipped = zip_insufficient = zip_failed = 0
    if do_zips:
        zip_keys = [args.postal] if args.postal else sorted(
            zip_groups.keys(), key=lambda z: -len(zip_groups[z]))
        if args.limit:
            zip_keys = zip_keys[:args.limit]
        print(f"🗺️  {len(zip_keys):,} ZIPs to process  (dry-run={args.dry_run})")

        pending: list[UpdateOne] = []
        for i, z in enumerate(zip_keys, 1):
            grp = zip_groups.get(z, [])
            cities = [l.get("city") for l in grp if l.get("city")]
            geo = {"postalCode": z, "city": Counter(cities).most_common(1)[0][0] if cities else None}
            try:
                stats = assemble_seasonal("zip", geo, grp)
            except Exception as e:
                zip_failed += 1
                print(f"  [ERR] ZIP {z}: {e}")
                continue
            if stats is None:
                zip_skipped += 1
                continue
            conf = stats["quality"]["confidence"]
            if conf == "insufficient":
                zip_insufficient += 1
            elif args.verbose or conf != "insufficient":
                print(f"Processing zip {i}/{len(zip_keys)}: {str(z):<8} "
                      f"{str(geo['city'])[:20]:<20} {stats['sampleSize']:>3} tiered  "
                      f"peak ${stats['peakRate'] or 0:,}  off ${stats['offPeakRate'] or 0:,}  "
                      f"ann ${stats['annualizedMonthly'] or 0:,}  [{conf}]")
            if args.dry_run and i <= 2:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run:
                # rent_rates docs are keyed by postalCode and owned by build-rent-rates.py;
                # only the `seasonal` sub-document belongs to this script.
                pending.append(UpdateOne({"postalCode": z},
                                         {"$set": {"seasonal": stats},
                                          "$setOnInsert": {"postalCode": z}},
                                         upsert=True))
                if len(pending) >= BULK_BATCH:
                    rent_rates_coll.bulk_write(pending, ordered=False)
                    zip_written += len(pending)
                    pending.clear()
        if not args.dry_run and pending:
            rent_rates_coll.bulk_write(pending, ordered=False)
            zip_written += len(pending)

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    if do_subs:
        print(f"Subdivisions — written {sub_written:,}  skipped {sub_skipped:,}  "
              f"insufficient {sub_insufficient:,}  failed {sub_failed:,}")
    if do_zips:
        print(f"ZIPs         — written {zip_written:,}  skipped {zip_skipped:,}  "
              f"insufficient {zip_insufficient:,}  failed {zip_failed:,}")
    print(f"Elapsed:       {elapsed/60:.1f} min" + ("  (dry-run, no writes)" if args.dry_run else ""))
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
