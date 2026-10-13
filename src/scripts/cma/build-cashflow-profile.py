#!/usr/bin/env python3
"""
Build Cash-Flow Profile  (the rollup)

Writes `cashflowProfile` onto subdivisions and cities — the single document an
MCP tool reads with one findOne by slug to answer "what could cash-flow in Palm
Desert Country Club" with no request-time aggregation and no arithmetic.

This stage COMPOSES; it computes no new economics. Every figure is a median over
the per-listing `cashflowStats` that build-cashflow.py already wrote, so the
rollup can never disagree with the listing detail a user drills into.

Inputs it stitches together:
  cmaStats.active            build-subdivision-cma.py   price/DOM aggregates
  inventoryProfile           build-inventory-profile.py bedMix + pool prevalence
  rentStats / rent_rates     build-rent-rates.py        long-term going rate
  rentStats.seasonal         build-seasonal-rates.py    seasonal, by bedroom
  rentStats.poolPremium      build-pool-premium.py      private-pool rent lift
  cashflowStats              build-cashflow.py          breakeven + scenarios

── Fallback and honesty rules ────────────────────────────────────────────────
* Seasonal: subdivision block if its basis is "tiered-median", else the ZIP block
  via subdivisions.dominantPostalCode, else absent. `rents.seasonal.source`
  records which was used so the answer can say so out loud.
* Pool premium: ZIP is PREFERRED over subdivision, because subdivision-level
  rows include the placeholder name "Other" (build-listing-cma.py documents the
  same placeholder problem) which groups unrelated properties together.
* `verdict.shortTerm` is always unavailable with a reason. No MLS source carries
  nightly short-term rates, so a modelled number there would be invention.
* A strategy with no rent signal is "unclear", never "negative". Those are
  different answers to an investor.

Usage:
    python3 src/scripts/cma/build-cashflow-profile.py --all
    python3 src/scripts/cma/build-cashflow-profile.py --slug palm-desert-country-club --dry-run
    python3 src/scripts/cma/build-cashflow-profile.py --city "Palm Desert" --verbose
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
BULK_BATCH = 100
TOP_LISTINGS = 10
PLACEHOLDER_SUBS = {"other", "unknown", "n/a", "none", "not in a development"}
SALE_MATCH = {"$or": [{"propertyType": "A"}, {"propertyTypeName": "Residential"}]}

LISTING_PROJECTION = {
    "listingKey": 1, "city": 1, "postalCode": 1, "subdivisionName": 1, "slug": 1,
    "bedsTotal": 1, "bedroomsTotal": 1, "listPrice": 1, "livingArea": 1,
    "unparsedAddress": 1, "cashflowStats": 1,
}

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=600000)
db = client.get_database()
subs_coll = db.subdivisions
cities_coll = db.cities
listings_coll = db.unified_listings
rent_rates_coll = db.rent_rates
print("✅ Connected to MongoDB")


def _escape_regex(s: str) -> str:
    return re.escape(s)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _med(vals, nd=2):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(statistics.median(v), nd) if v else None


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


# ──────────────────────────────────────────────────────────────────────────────
# 🧮 COMPOSITION
# ──────────────────────────────────────────────────────────────────────────────


def _strategy_medians(listings: list[dict], path: str) -> dict | None:
    """Median of the stored per-listing figures for one strategy.

    path is "" for the long-term block (top level of cashflowStats) or
    "seasonal" for strategies.seasonal.
    """
    rows = []
    for l in listings:
        cf = l.get("cashflowStats") or {}
        blk = cf if path == "" else ((cf.get("strategies") or {}).get(path) or {})
        if not blk or not blk.get("scenarios"):
            continue
        d20 = blk["scenarios"].get("down20") or {}
        rows.append({
            "rent": _num((blk.get("rentEstimate") or {}).get("monthlyRent")),
            "be": _num(blk.get("breakEvenDownPct")),
            "cf20": _num(d20.get("monthlyCashflow")),
            "coc20": _num(d20.get("cashOnCashPct")),
            "dscr20": _num(d20.get("dscr")),
            "cashflows": bool(d20.get("cashflows")),
            "cap": _num(blk.get("capRatePct")),
        })
    if not rows:
        return None
    bes = [r["be"] for r in rows if r["be"] is not None]
    return {
        "listingsScored": len(rows),
        "rentUsed": _med(r["rent"] for r in rows),
        "breakEvenDownPct": _med(bes, 4),
        "minBreakEvenDownPct": round(min(bes), 4) if bes else None,
        "neverCashflowsCount": sum(1 for r in rows if r["be"] is None),
        "monthlyCashflowAt20": _med(r["cf20"] for r in rows),
        "cashOnCashAt20Pct": _med(r["coc20"] for r in rows),
        "dscrAt20": _med(r["dscr20"] for r in rows),
        "capRatePct": _med(r["cap"] for r in rows),
        "cashflowingAt20Count": sum(1 for r in rows if r["cashflows"]),
    }


def _by_bed_economics(listings: list[dict]) -> list[dict]:
    g = defaultdict(list)
    for l in listings:
        b = beds_bucket(l)
        if b is not None:
            g[b].append(l)
    out = []
    for b in sorted(g):
        grp = g[b]
        out.append({
            "beds": b,
            "bedsLabel": beds_label(b),
            "count": len(grp),
            "archetypePrice": _med(x.get("listPrice") for x in grp),
            "medianSqft": _med(x.get("livingArea") for x in grp),
            "longTerm": _strategy_medians(grp, ""),
            "seasonal": _strategy_medians(grp, "seasonal"),
        })
    return out


def _verdict(block: dict | None, label: str) -> dict:
    """A strategy with no rent signal is 'unclear'. One with a signal and no
    achievable down payment is 'negative'. These are different answers."""
    if not block:
        return {"status": "unclear",
                "reason": f"no {label} rent signal for this area at the required confidence"}
    if block["cashflowingAt20Count"] > 0:
        return {"status": "cashflows",
                "reason": f"{block['cashflowingAt20Count']} of {block['listingsScored']} "
                          f"listings cash-flow at 20% down",
                "medianMonthlyAt20": block["monthlyCashflowAt20"]}
    be = block["minBreakEvenDownPct"]
    if be is None:
        return {"status": "negative",
                "reason": "operating costs exceed effective rent on every listing; "
                          "no down payment makes these cash-flow"}
    return {"status": "negative",
            "reason": f"none cash-flow at 20% down; the best listing needs {be:.0%} down",
            "minBreakEvenDownPct": be}


def _seasonal_rents(sub_rs: dict, zip_doc: dict | None) -> dict:
    """subdivision -> ZIP -> absent, recording which was used."""
    s = (sub_rs or {}).get("seasonal") or {}
    if s.get("basis") == "tiered-median":
        return {"source": "subdivision", **{k: s.get(k) for k in
                ("basis", "sampleSize", "peakRate", "offPeakRate",
                 "annualizedMonthly", "peakMonths", "byBed", "quality")}}
    z = (zip_doc or {}).get("seasonal") or {}
    if z.get("basis") == "tiered-median":
        return {"source": "zip", "zipUsed": (z.get("geo") or {}).get("postalCode"),
                **{k: z.get(k) for k in
                   ("basis", "sampleSize", "peakRate", "offPeakRate",
                    "annualizedMonthly", "peakMonths", "byBed", "quality")}}
    return {"source": "insufficient", "basis": "insufficient",
            "sampleSize": (s.get("sampleSize") or 0),
            "note": "too few tiered lease comps here and in the fallback ZIP to "
                    "state a seasonal rate"}


def _pool_premium(sub_rs: dict, sub_name: str, zip_doc: dict | None) -> dict:
    """ZIP preferred — subdivision rows include the placeholder name 'Other',
    which lumps unrelated properties together."""
    z = (zip_doc or {}).get("poolPremium") or {}
    if z.get("basis") == "matched-cell-median":
        return {"source": "zip", "zipUsed": (z.get("geo") or {}).get("postalCode"),
                **{k: z.get(k) for k in ("basis", "overallPctLift", "sampleSize",
                                         "byBed", "method", "quality")}}
    s = (sub_rs or {}).get("poolPremium") or {}
    if (s.get("basis") == "matched-cell-median"
            and (sub_name or "").strip().lower() not in PLACEHOLDER_SUBS):
        return {"source": "subdivision",
                **{k: s.get(k) for k in ("basis", "overallPctLift", "sampleSize",
                                         "byBed", "method", "quality")}}
    return {"source": "insufficient", "basis": "insufficient",
            "note": "no bedroom+subType cell had enough private-pool and no-pool "
                    "leases to isolate a private-pool premium here (common in "
                    "country-club areas, where nearly every pool is association-owned)"}


def assemble_profile(scope: str, geo: dict, listings: list[dict],
                     cma_active: dict, inv: dict, sub_rs: dict,
                     zip_doc: dict | None) -> dict | None:
    if not listings:
        return None

    lt = _strategy_medians(listings, "")
    se = _strategy_medians(listings, "seasonal")

    # Representative economics come from whichever listing carries them; the rate
    # is identical across a build so the first is representative.
    rate = rate_as_of = assumptions = None
    for l in listings:
        a = (l.get("cashflowStats") or {}).get("assumptions")
        if a:
            rate = a.get("mortgageRate")
            rate_as_of = (l.get("cashflowStats") or {}).get("lastUpdated")
            assumptions = {k: a.get(k) for k in
                           ("propertyTaxRate", "insuranceRate", "vacancyPct",
                            "managementPct", "maintenancePct", "closingCostPct",
                            "loanTermYears", "rateSource")}
            break

    flowing = sorted(
        [l for l in listings
         if (((l.get("cashflowStats") or {}).get("scenarios") or {})
             .get("down20", {}).get("cashflows"))],
        key=lambda l: -(l["cashflowStats"]["scenarios"]["down20"]["monthlyCashflow"]))
    flowing_seasonal = sorted(
        [l for l in listings
         if ((((l.get("cashflowStats") or {}).get("strategies") or {}).get("seasonal") or {})
             .get("scenarios", {}).get("down20", {}).get("cashflows"))],
        key=lambda l: -(l["cashflowStats"]["strategies"]["seasonal"]
                        ["scenarios"]["down20"]["monthlyCashflow"]))

    def _ref(l):
        cf = l.get("cashflowStats") or {}
        return {"listingKey": l.get("listingKey"), "address": l.get("unparsedAddress"),
                "listPrice": l.get("listPrice"), "beds": beds_bucket(l),
                "slug": l.get("slug"),
                "breakEvenDownPct": cf.get("breakEvenDownPct"),
                "monthlyCashflowAt20": (cf.get("scenarios") or {})
                                       .get("down20", {}).get("monthlyCashflow")}

    notes: list[str] = []
    seasonal = _seasonal_rents(sub_rs, zip_doc)
    if seasonal["source"] == "zip":
        notes.append(f"seasonal rates come from ZIP {seasonal.get('zipUsed')}, not the "
                     f"subdivision itself — too few tiered lease comps locally")
    pool = _pool_premium(sub_rs, geo.get("name") or geo.get("subdivisionName") or "", zip_doc)
    if pool["source"] == "insufficient":
        notes.append("no private-pool rent premium available for this area")
    if lt and lt["neverCashflowsCount"]:
        notes.append(f"{lt['neverCashflowsCount']} listings cannot cash-flow long-term at "
                     f"any down payment (operating costs exceed effective rent)")

    confidence = "good" if (lt and lt["listingsScored"] >= 8) else (
        "fair" if lt else "insufficient")

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "geo": geo,
        "inventory": {
            "activeCount": len(listings),
            "medianListPrice": _med(l.get("listPrice") for l in listings),
            "medianSqft": _med(l.get("livingArea") for l in listings),
            "medianPricePerSqft": (cma_active or {}).get("medianPricePerSqft"),
            "avgDom": (cma_active or {}).get("avgDom"),
            "minPrice": (cma_active or {}).get("minPrice"),
            "maxPrice": (cma_active or {}).get("maxPrice"),
            "bedMix": (inv or {}).get("bedMix") or [],
            "poolPctPrivate": (inv or {}).get("poolPrevalence"),
            "poolPctAny": (inv or {}).get("poolPrevalenceAny"),
            "poolPctCommunity": (inv or {}).get("poolPrevalenceCommunity"),
            "poolBreakdown": (inv or {}).get("poolBreakdown"),
            "spaPrevalence": (inv or {}).get("spaPrevalence"),
            "gatedCommunity": (inv or {}).get("gatedCommunity"),
            "dominantSubType": (inv or {}).get("dominantSubType"),
        },
        "rents": {
            "longTerm": {
                "basis": (sub_rs or {}).get("goingRate", {}).get("basis"),
                "monthlyRent": (sub_rs or {}).get("goingRate", {}).get("monthlyRent"),
                "sampleSize": (sub_rs or {}).get("goingRate", {}).get("sampleSize"),
                "byBed": (sub_rs or {}).get("byBedroom") or [],
                "confidence": (sub_rs or {}).get("quality", {}).get("confidence"),
            },
            "seasonal": seasonal,
            "poolPremium": pool,
        },
        "economics": {
            "mortgageRatePct": round(rate * 100, 3) if rate else None,
            "rateAsOf": rate_as_of,
            "assumptions": assumptions,
            "longTerm": lt,
            "seasonal": se,
            "byBed": _by_bed_economics(listings),
        },
        "verdict": {
            "longTerm": _verdict(lt, "long-term"),
            "seasonal": _verdict(se, "seasonal"),
            "shortTerm": {"status": "unclear",
                          "reason": "no MLS source carries nightly short-term rates; "
                                    "verify HOA minimum-lease terms and the city's "
                                    "short-term-rental ordinance"},
            "notes": notes,
        },
        "cashflowingAt20": {
            "longTermCount": len(flowing),
            "seasonalCount": len(flowing_seasonal),
            "topLongTerm": [_ref(l) for l in flowing[:TOP_LISTINGS]],
            "topSeasonal": [_ref(l) for l in flowing_seasonal[:TOP_LISTINGS]],
        },
        "quality": {"confidence": confidence, "notes": notes},
    }


def main():
    p = argparse.ArgumentParser(description="Build cashflowProfile on subdivisions + cities.")
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
    print("📥 Loading active sale listings with cashflowStats…")
    listings = list(listings_coll.find(q, LISTING_PROJECTION))
    print(f"   {len(listings):,} listings")

    zip_index = {z["postalCode"]: z for z in rent_rates_coll.find({})}
    print(f"   {len(zip_index):,} ZIP rent docs")

    by_sub = defaultdict(list)
    by_city = defaultdict(list)
    for l in listings:
        name = (l.get("subdivisionName") or "").strip().lower()
        city = (l.get("city") or "").strip().lower()
        if name and city:
            by_sub[(city, name)].append(l)
        if city:
            by_city[city].append(l)

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
            geo = {"name": sub.get("name"), "slug": sub.get("slug"), "city": sub.get("city"),
                   "county": sub.get("county"),
                   "dominantPostalCode": sub.get("dominantPostalCode")}
            prof = assemble_profile(
                "subdivision", geo, by_sub.get(key, []),
                (sub.get("cmaStats") or {}).get("active") or {},
                sub.get("inventoryProfile") or {},
                sub.get("rentStats") or {},
                zip_index.get(sub.get("dominantPostalCode")))
            if prof is None:
                sub_skipped += 1
                continue
            if args.verbose or args.dry_run:
                v = prof["verdict"]
                print(f"  sub {i}/{len(subs)}: {str(sub.get('name'))[:30]:<30} "
                      f"n={prof['inventory']['activeCount']:<4} "
                      f"LT={v['longTerm']['status']:<10} SEAS={v['seasonal']['status']:<10} "
                      f"[{prof['quality']['confidence']}]")
            if args.dry_run and i <= 1:
                print(json.dumps(prof, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"_id": sub["_id"]},
                                         {"$set": {"cashflowProfile": prof}}))
                if len(pending) >= BULK_BATCH:
                    sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count

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
            grp = by_city.get(key, [])
            if not grp:
                continue
            # A city's long-term rent block lives on the ZIP docs, not on the city
            # doc, so the city rollup leans on its listings' stored estimates.
            geo = {"name": city.get("name"), "slug": city.get("slug"),
                   "city": city.get("name"), "county": city.get("county")}
            zc = Counter(l.get("postalCode") for l in grp if l.get("postalCode"))
            dom = zc.most_common(1)[0][0] if zc else None
            geo["dominantPostalCode"] = dom
            prof = assemble_profile("city", geo, grp, {},
                                    city.get("inventoryProfile") or {},
                                    {}, zip_index.get(dom))
            if prof is None:
                continue
            if not args.dry_run:
                pending.append(UpdateOne({"_id": city["_id"]},
                                         {"$set": {"cashflowProfile": prof}}))
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
