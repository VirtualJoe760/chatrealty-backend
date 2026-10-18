#!/usr/bin/env python3
"""
Build Inventory Profile

Adds the two things a cash-flow answer needs about the FOR-SALE inventory that
build-subdivision-cma.py does not produce:

  1. cmaStats.active.bedMix — a histogram, not an average. cmaStats.active today
     carries only avgBeds (2.88) / typicalBeds (3), which cannot answer "how many
     2/2 versus 3/2 are for sale".
  2. cmaStats.subdivisionProfile.{poolPrevalence, spaPrevalence, gatedCommunity,
     seniorCommunity} — four fields that ALREADY EXIST on the profile and are
     null everywhere. garagePrevalence is populated at 0.6316, so the shape to
     match is already in build-subdivision-cma.py.

Runs as its own backfill rather than inside build-subdivision-cma.py, which takes
112 minutes; this does the same scopes in about a minute because it needs only a
handful of fields per listing.

── Denominator discipline ─────────────────────────────────────────────────────
A prevalence is computed over the listings where the source field is READABLE,
never over all listings, and the denominator is reported alongside it. Otherwise
a subdivision where 80% of listings omit poolFeatures reads as "20% have pools"
when the truth is "we don't know for 80% of them". Each prevalence therefore
ships as {pct, withData, total}.

Pools reuse the private / shared / none / unknown classification from
build-pool-premium.py: `poolPrevalence` is the share with a PRIVATE pool, and the
community split is kept beside it, because in a country-club community almost
every pool is association-owned and conflating the two is misleading.

Writes to subdivisions and cities (both carry a `slug`).

Usage:
    python3 src/scripts/cma/build-inventory-profile.py --all
    python3 src/scripts/cma/build-inventory-profile.py --slug palm-desert-country-club --dry-run
    python3 src/scripts/cma/build-inventory-profile.py --city "Palm Desert" --verbose
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

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

MAX_BEDS_BUCKET = 5
BULK_BATCH = 200
SALE_MATCH = {"$or": [{"propertyType": "A"}, {"propertyTypeName": "Residential"}]}

PROJECTION = {
    "listingKey": 1, "city": 1, "postalCode": 1, "subdivisionName": 1,
    "bedsTotal": 1, "bedroomsTotal": 1, "bathroomsTotalInteger": 1, "bathroomsTotal": 1,
    "livingArea": 1, "listPrice": 1, "propertySubType": 1,
    "poolFeatures": 1, "spaFeatures": 1, "communityFeatures": 1,
    "associationAmenities": 1, "seniorCommunityYN": 1, "seniorCommunity": 1,
}

SHARED_TOKENS = ("community", "association")
NONE_TOKENS = ("none",)
PRIVATE_TOKENS = ("private", "in ground", "inground", "above ground", "heated",
                  "lap", "salt water", "saltwater", "filtered", "tile", "gunite",
                  "fenced", "waterfall", "infinity", "exercise", "solar heat", "pebble")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=600000)
db = client.get_database()
subs_coll = db.subdivisions
cities_coll = db.cities
listings_coll = db.unified_listings
print("✅ Connected to MongoDB")


def _escape_regex(s: str) -> str:
    return re.escape(s)


def feat_tokens(raw) -> list[str]:
    """poolFeatures / spaFeatures / communityFeatures arrive as a comma string OR
    a dict OR a list, depending on the MLS. Normalize all three."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [str(k).strip().lower() for k, v in raw.items() if v]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip().lower() for x in raw if x]
    return [t.strip().lower() for t in str(raw).split(",") if t.strip()]


def classify_pool(raw) -> str:
    toks = feat_tokens(raw)
    if not toks:
        return "unknown"
    if any(t in NONE_TOKENS or t.startswith("none") for t in toks):
        return "none"
    if any(any(s in t for s in SHARED_TOKENS) for t in toks):
        return "shared"
    if any(any(p in t for p in PRIVATE_TOKENS) for t in toks):
        return "private"
    return "unknown"


def has_spa(raw) -> bool | None:
    toks = feat_tokens(raw)
    if not toks:
        return None
    if any(t in NONE_TOKENS or t.startswith("none") for t in toks):
        return False
    return True


def is_gated(l: dict) -> bool | None:
    toks = feat_tokens(l.get("communityFeatures")) + feat_tokens(l.get("associationAmenities"))
    if not toks:
        return None
    return any("gate" in t for t in toks)


def is_senior(l: dict) -> bool | None:
    for f in ("seniorCommunityYN", "seniorCommunity"):
        v = l.get(f)
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip():
            s = v.strip().lower()
            if s in ("yes", "true", "y", "1"):
                return True
            if s in ("no", "false", "n", "0"):
                return False
    return None


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


def _med(vals):
    v = [x for x in vals if isinstance(x, (int, float)) and x > 0]
    return round(statistics.median(v), 2) if v else None


def _prevalence(flags: list) -> dict:
    """flags may contain True / False / None. None means 'no data for this listing'
    and is excluded from the denominator but reported."""
    known = [f for f in flags if f is not None]
    if not known:
        return {"pct": None, "withData": 0, "total": len(flags)}
    return {"pct": round(sum(1 for f in known if f) / len(known), 4),
            "withData": len(known), "total": len(flags)}


def assemble(scope: str, geo: dict, listings: list[dict]) -> dict | None:
    if not listings:
        return None

    # ── bed mix histogram ────────────────────────────────────────────────────
    g = defaultdict(list)
    for l in listings:
        b = beds_bucket(l)
        if b is not None:
            g[b].append(l)
    total_binned = sum(len(v) for v in g.values())
    bed_mix = []
    for b in sorted(g):
        grp = g[b]
        bed_mix.append({
            "beds": b,
            "bedsLabel": beds_label(b),
            "count": len(grp),
            "pct": round(len(grp) / total_binned, 4) if total_binned else None,
            "medianListPrice": _med(x.get("listPrice") for x in grp),
            "medianSqft": _med(x.get("livingArea") for x in grp),
            "medianBaths": _med(x.get("bathroomsTotalInteger") or x.get("bathroomsTotal")
                                for x in grp),
        })

    # ── prevalences ──────────────────────────────────────────────────────────
    pool_classes = [classify_pool(l.get("poolFeatures")) for l in listings]
    pc = Counter(pool_classes)
    readable = pc["private"] + pc["shared"] + pc["none"]
    private_prev = round(pc["private"] / readable, 4) if readable else None
    shared_prev = round(pc["shared"] / readable, 4) if readable else None
    any_prev = round((pc["private"] + pc["shared"]) / readable, 4) if readable else None

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "geo": geo,
        "activeCount": len(listings),
        "bedMix": bed_mix,
        "bedMixBinned": total_binned,
        "poolPrevalence": private_prev,          # share with a PRIVATE pool
        "poolPrevalenceAny": any_prev,           # private OR community
        "poolPrevalenceCommunity": shared_prev,
        "poolBreakdown": {"private": pc["private"], "community": pc["shared"],
                          "none": pc["none"], "unknown": pc["unknown"],
                          "withData": readable, "total": len(listings)},
        "spaPrevalence": _prevalence([has_spa(l.get("spaFeatures")) for l in listings]),
        "gatedCommunity": _prevalence([is_gated(l) for l in listings]),
        "seniorCommunity": _prevalence([is_senior(l) for l in listings]),
        "dominantSubType": (Counter(
            (l.get("propertySubType") or "Unknown") for l in listings
        ).most_common(1)[0][0]),
    }


def group_by_sub(items):
    g = defaultdict(list)
    for l in items:
        name = (l.get("subdivisionName") or "").strip().lower()
        city = (l.get("city") or "").strip().lower()
        if name and city:
            g[(city, name)].append(l)
    return g


def group_by_city(items):
    g = defaultdict(list)
    for l in items:
        city = (l.get("city") or "").strip().lower()
        if city:
            g[city].append(l)
    return g


def main():
    p = argparse.ArgumentParser(description="Backfill bedMix + profile prevalences.")
    p.add_argument("--all", action="store_true")
    p.add_argument("--slug")
    p.add_argument("--subdivision", dest="subdivision_name")
    p.add_argument("--city")
    p.add_argument("--county")
    p.add_argument("--subs-only", action="store_true")
    p.add_argument("--cities-only", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not (args.slug or args.subdivision_name or args.city or args.county or args.all):
        print("[ERROR] Must pass --all, --slug, --subdivision, --city, or --county.", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    started = time.time()
    q = {"standardStatus": "Active", **SALE_MATCH}
    if args.city:
        q["city"] = args.city
    print("📥 Loading active sale listings…")
    listings = list(listings_coll.find(q, PROJECTION))
    print(f"   {len(listings):,} active residential sale listings")

    sub_groups = group_by_sub(listings)
    city_groups = group_by_city(listings)

    # ── subdivisions ─────────────────────────────────────────────────────────
    sub_written = sub_skipped = 0
    if not args.cities_only:
        sq: dict = {}
        if args.slug:
            sq["slug"] = args.slug
        elif args.subdivision_name:
            sq["name"] = {"$regex": f"^{_escape_regex(args.subdivision_name)}$", "$options": "i"}
        elif args.city:
            sq["city"] = args.city
        elif args.county:
            sq["county"] = {"$regex": f"^{_escape_regex(args.county)}$", "$options": "i"}
        cur = subs_coll.find(sq)
        if args.limit:
            cur = cur.limit(args.limit)
        subs = list(cur)
        print(f"🏘️  {len(subs):,} subdivisions  (dry-run={args.dry_run})")
        pending: list[UpdateOne] = []
        for i, sub in enumerate(subs, 1):
            key = ((sub.get("city") or "").strip().lower(), (sub.get("name") or "").strip().lower())
            geo = {"subdivisionName": sub.get("name"), "slug": sub.get("slug"),
                   "city": sub.get("city"), "county": sub.get("county")}
            prof = assemble("subdivision", geo, sub_groups.get(key, []))
            if prof is None:
                sub_skipped += 1
                continue
            if args.verbose or args.dry_run:
                mix = " ".join(f"{m['bedsLabel']}bd×{m['count']}" for m in prof["bedMix"])
                pp = prof["poolPrevalence"]
                print(f"  sub {i}/{len(subs)}: {str(sub.get('name'))[:34]:<34} "
                      f"n={prof['activeCount']:<4} {mix:<34} "
                      f"privPool={'—' if pp is None else f'{pp:.0%}'}")
            if args.dry_run and i <= 2:
                print(json.dumps(prof, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"_id": sub["_id"]}, {"$set": {
                    "cmaStats.active.bedMix": prof["bedMix"],
                    "cmaStats.subdivisionProfile.poolPrevalence": prof["poolPrevalence"],
                    "cmaStats.subdivisionProfile.poolPrevalenceAny": prof["poolPrevalenceAny"],
                    "cmaStats.subdivisionProfile.poolPrevalenceCommunity": prof["poolPrevalenceCommunity"],
                    "cmaStats.subdivisionProfile.poolBreakdown": prof["poolBreakdown"],
                    "cmaStats.subdivisionProfile.spaPrevalence": prof["spaPrevalence"],
                    "cmaStats.subdivisionProfile.gatedCommunity": prof["gatedCommunity"],
                    "cmaStats.subdivisionProfile.seniorCommunity": prof["seniorCommunity"],
                    "inventoryProfile": prof,
                }}))
                if len(pending) >= BULK_BATCH:
                    sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count

    # ── cities ───────────────────────────────────────────────────────────────
    city_written = 0
    if not args.subs_only:
        cq: dict = {}
        if args.city:
            cq["name"] = {"$regex": f"^{_escape_regex(args.city)}$", "$options": "i"}
        cur = cities_coll.find(cq)
        if args.limit:
            cur = cur.limit(args.limit)
        cities = list(cur)
        print(f"🌆 {len(cities):,} cities  (dry-run={args.dry_run})")
        pending = []
        for city in cities:
            key = (city.get("name") or "").strip().lower()
            geo = {"city": city.get("name"), "slug": city.get("slug"),
                   "county": city.get("county")}
            prof = assemble("city", geo, city_groups.get(key, []))
            if prof is None:
                continue
            if not args.dry_run:
                pending.append(UpdateOne({"_id": city["_id"]},
                                         {"$set": {"inventoryProfile": prof}}))
                if len(pending) >= BULK_BATCH:
                    city_written += cities_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            city_written += cities_coll.bulk_write(pending, ordered=False).modified_count

    el = time.time() - started
    print("\n" + "=" * 60)
    print(f"Subdivisions — written {sub_written:,}  skipped (no inventory) {sub_skipped:,}")
    print(f"Cities       — written {city_written:,}")
    print(f"Elapsed:       {el/60:.1f} min" + ("  (dry-run, no writes)" if args.dry_run else ""))
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
