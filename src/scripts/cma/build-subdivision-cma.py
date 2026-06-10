#!/usr/bin/env python3
"""
Build Subdivision CMA

Nightly aggregator: for every doc in the `subdivisions` collection, reads
sale listings (Residential only, no leases) from `unified_listings` and
`unified_closed_listings`, computes a `cmaStats` block, and writes it back
onto the subdivision doc.

Scope decision (session [120]): this builder ONLY touches existing
subdivisions. It does NOT auto-seed new docs, NOT canonicalize aliases, NOT
strip the legacy `avgPrice`/`listingCount`/`propertyTypes` fields. Those
are separate cleanup passes. The ~43% orphan-name rate means some listings
won't contribute to any subdivision's CMA until that cleanup runs — a
known limitation, not a bug.

Match rule: case-insensitive exact match of `unified_listings.subdivisionName`
against `subdivisions.name` within the same `city`. No fuzzy, no aliases.

Usage:
    # One subdivision, no DB writes
    python3 src/scripts/cma/build-subdivision-cma.py --slug indian-wells-country-club --dry-run

    # One city
    python3 src/scripts/cma/build-subdivision-cma.py --city "Palm Desert"

    # Full run (cron)
    python3 src/scripts/cma/build-subdivision-cma.py --all
"""

import os
import sys
import json
import time
import argparse
import statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
from pymongo import MongoClient, UpdateOne
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
CLOSING_CAP = 25
SAMPLE_WINDOW_MONTHS = 12
INSUFFICIENT_CLOSED_IN_WINDOW = 3   # If fewer than this & <3 actives → insufficient
INSUFFICIENT_ACTIVE = 3
MAJORITY_THRESHOLD = 0.60           # ≥60% amenity prevalence ⇒ "X community"
TOP_COMPS = 5
BULK_BATCH = 200

RESIDENTIAL_PROPERTY_TYPE = "A"     # per session [134]

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
subs_coll = db.subdivisions
listings_coll = db.unified_listings
closed_coll = db.unified_closed_listings
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────


def coerce_date(v):
    """closeDate is mixed datetime / str in the collection — normalize to datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def is_lease(listing: dict) -> bool:
    pt = (listing.get("propertyType") or "").strip()
    # Residential-only filter (session [134]): only keep "A"
    if pt and pt != RESIDENTIAL_PROPERTY_TYPE:
        return True
    if (listing.get("propertyTypeName") or "") == "Residential Lease":
        return True
    land = (listing.get("landType") or "").strip().lower()
    if "lease" in land:
        return True
    return False


def days_on_market(listing: dict, reference: datetime | None = None) -> int | None:
    """daysOnMarket is a Mongoose virtual — compute it from onMarketDate."""
    dom = listing.get("daysOnMarket")
    if isinstance(dom, (int, float)) and dom >= 0:
        return int(dom)
    omd = coerce_date(listing.get("onMarketDate"))
    if not omd:
        return None
    end = reference or coerce_date(listing.get("closeDate")) or datetime.now(timezone.utc)
    delta = (end - omd).days
    return delta if delta >= 0 else None


def _safe_stat(values: list[float], fn):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    try:
        return round(fn(vals), 2)
    except statistics.StatisticsError:
        return None


def _ppsf(price, sqft):
    if not price or not sqft or sqft <= 0:
        return None
    return price / sqft


def _percentile(values: list[float], p: float) -> float | None:
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 2)
    k = (len(vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    return round(vals[lo] + (vals[hi] - vals[lo]) * frac, 2)


def _mode_or_none(values: list):
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


def _prevalence(values: list[bool | None]) -> float | None:
    observed = [v for v in values if v is not None]
    if not observed:
        return None
    trues = sum(1 for v in observed if bool(v))
    return trues / len(observed)


def _month_bucket(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"

# ──────────────────────────────────────────────────────────────────────────────
# 🧮 STAT BLOCKS
# ──────────────────────────────────────────────────────────────────────────────


def build_active_block(actives: list[dict]) -> dict:
    """Active-listing aggregates. Spec field names; avgSqft + avg beds/baths kept as extras."""
    prices = [l.get("listPrice") for l in actives if l.get("listPrice")]
    sqfts = [l.get("livingArea") for l in actives if l.get("livingArea")]
    ppsfs = [_ppsf(l.get("listPrice"), l.get("livingArea")) for l in actives]
    ppsfs = [p for p in ppsfs if p]
    beds = [l.get("bedsTotal") for l in actives if l.get("bedsTotal")]
    baths = [l.get("bathsTotal") for l in actives if l.get("bathsTotal")]
    doms = [days_on_market(l) for l in actives]
    doms = [d for d in doms if d is not None]

    return {
        "count": len(actives),
        "medianPrice": _safe_stat(prices, statistics.median),
        "avgPrice": _safe_stat(prices, statistics.mean),
        "minPrice": min(prices) if prices else None,
        "maxPrice": max(prices) if prices else None,
        "medianPricePerSqft": _safe_stat(ppsfs, statistics.median),
        "avgDom": _safe_stat(doms, statistics.mean),
        "medianSqft": _safe_stat(sqfts, statistics.median),
        # extras
        "avgSqft": _safe_stat(sqfts, statistics.mean),
        "avgPricePerSqft": _safe_stat(ppsfs, statistics.mean),
        "avgBeds": _safe_stat(beds, statistics.mean),
        "avgBaths": _safe_stat(baths, statistics.mean),
    }


def build_closed_block(closed: list[dict]) -> dict:
    """Closed-listing aggregates. Spec field names; min/max + extras kept alongside."""
    if not closed:
        return {"count": 0}

    close_dates = [coerce_date(l.get("closeDate")) for l in closed]
    close_dates = [d for d in close_dates if d]
    start = min(close_dates) if close_dates else None
    end = max(close_dates) if close_dates else None

    sale_prices = [l.get("closePrice") for l in closed if l.get("closePrice")]
    ppsfs = [_ppsf(l.get("closePrice"), l.get("livingArea")) for l in closed]
    ppsfs = [p for p in ppsfs if p]
    doms = [days_on_market(l, coerce_date(l.get("closeDate"))) for l in closed]
    doms = [d for d in doms if d is not None]

    stl_ratios = []
    reductions = []
    for l in closed:
        close_price = l.get("closePrice")
        orig = l.get("originalListPrice") or l.get("listPrice")
        if close_price and orig and orig > 0:
            stl_ratios.append(close_price / orig)
            reductions.append((orig - close_price) / orig)

    return {
        "count": len(closed),
        "medianClosePrice": _safe_stat(sale_prices, statistics.median),
        "avgClosePrice": _safe_stat(sale_prices, statistics.mean),
        "medianPricePerSqft": _safe_stat(ppsfs, statistics.median),
        "avgDom": _safe_stat(doms, statistics.mean),
        "saleToListRatio": _safe_stat(stl_ratios, statistics.mean),
        # extras
        "minClosePrice": min(sale_prices) if sale_prices else None,
        "maxClosePrice": max(sale_prices) if sale_prices else None,
        "avgPricePerSqft": _safe_stat(ppsfs, statistics.mean),
        "avgPriceReductionPct": _safe_stat(reductions, statistics.mean),
        "sampleStartDate": start.isoformat() if start else None,
        "sampleEndDate": end.isoformat() if end else None,
    }


def build_by_subtype(actives: list[dict], closed: list[dict]) -> list[dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"actives": [], "closed": []})
    for l in actives:
        key = l.get("propertySubType") or "Unknown"
        groups[key]["actives"].append(l)
    for l in closed:
        key = l.get("propertySubType") or "Unknown"
        groups[key]["closed"].append(l)

    out = []
    for sub_type, entry in groups.items():
        c = entry["closed"]
        sale_prices = [x.get("closePrice") for x in c if x.get("closePrice")]
        ppsfs = [_ppsf(x.get("closePrice"), x.get("livingArea")) for x in c]
        ppsfs = [p for p in ppsfs if p]
        doms = [days_on_market(x, coerce_date(x.get("closeDate"))) for x in c]
        doms = [d for d in doms if d is not None]
        stl = []
        for x in c:
            cp = x.get("closePrice"); op = x.get("originalListPrice") or x.get("listPrice")
            if cp and op and op > 0:
                stl.append(cp / op)
        cd = [coerce_date(x.get("closeDate")) for x in c]
        cd = [d for d in cd if d]
        out.append({
            "subType": sub_type,
            "activeCount": len(entry["actives"]),
            "closedCount": len(c),
            "medianSalePrice": _safe_stat(sale_prices, statistics.median),
            "avgSalePpsf": _safe_stat(ppsfs, statistics.mean),
            "avgDom": _safe_stat(doms, statistics.mean),
            "avgSaleToListRatio": _safe_stat(stl, statistics.mean),
            "sampleStartDate": min(cd).isoformat() if cd else None,
            "sampleEndDate": max(cd).isoformat() if cd else None,
        })
    out.sort(key=lambda r: (r["closedCount"] + r["activeCount"]), reverse=True)
    return out


def build_subdivision_profile(actives: list[dict], closed: list[dict]) -> dict:
    """
    Community character. Spec-shape:
      poolPrevalence / spaPrevalence / garagePrevalence are raw fractions
      (0.0–1.0 or None). Frontend decides what to do with "unknown" listings.
      gatedCommunity / seniorCommunity are booleans (>50% → true). Extras
      (dominantSubType, typicalGarage, typicalBeds/Baths/Sqft/YearBuilt)
      kept alongside for future frontend use.
    """
    pop = actives + closed
    sub_types = [l.get("propertySubType") for l in pop]
    pools = [l.get("poolYn") for l in pop]
    spas = [l.get("spaYn") for l in pop]
    gated = [l.get("gatedCommunityYn") for l in pop]
    senior = [l.get("seniorCommunityYn") for l in pop]
    garage_spaces = [l.get("garageSpaces") for l in pop if l.get("garageSpaces") is not None]
    has_garage = [1 if (g and g > 0) else 0 for g in garage_spaces] if garage_spaces else []
    beds = [l.get("bedsTotal") for l in pop if l.get("bedsTotal") is not None]
    baths = [l.get("bathsTotal") for l in pop if l.get("bathsTotal") is not None]
    sqfts = [l.get("livingArea") for l in pop if l.get("livingArea")]
    yrs = [l.get("yearBuilt") for l in pop if l.get("yearBuilt")]

    def _prev_round(values):
        p = _prevalence(values)
        return None if p is None else round(p, 4)

    # Garage "prevalence" = fraction of listings with garageSpaces > 0
    garage_prevalence = None
    if has_garage:
        garage_prevalence = round(sum(has_garage) / len(has_garage), 4)

    def _majority_bool(values):
        p = _prevalence(values)
        return None if p is None else p > 0.50

    return {
        # Spec fields
        "poolPrevalence": _prev_round(pools),
        "spaPrevalence": _prev_round(spas),
        "garagePrevalence": garage_prevalence,
        "gatedCommunity": _majority_bool(gated),
        "seniorCommunity": _majority_bool(senior),
        # Extras (my original profile data)
        "dominantSubType": _mode_or_none(sub_types),
        "typicalGarage": _mode_or_none(garage_spaces),
        "typicalBeds": _mode_or_none(beds),
        "typicalBaths": _mode_or_none(baths),
        "typicalSqftRange": {
            "p25": _percentile(sqfts, 0.25),
            "median": _percentile(sqfts, 0.50),
            "p75": _percentile(sqfts, 0.75),
        } if sqfts else None,
        "typicalYearBuiltRange": {
            "p25": _percentile(yrs, 0.25),
            "median": _percentile(yrs, 0.50),
            "p75": _percentile(yrs, 0.75),
        } if yrs else None,
    }


def build_trends(closed: list[dict]) -> dict:
    monthly: dict[str, list[dict]] = defaultdict(list)
    for l in closed:
        cd = coerce_date(l.get("closeDate"))
        if cd:
            monthly[_month_bucket(cd)].append(l)

    rows = []
    for month in sorted(monthly):
        entries = monthly[month]
        sale_prices = [x.get("closePrice") for x in entries if x.get("closePrice")]
        ppsfs = [_ppsf(x.get("closePrice"), x.get("livingArea")) for x in entries]
        ppsfs = [p for p in ppsfs if p]
        doms = [days_on_market(x, coerce_date(x.get("closeDate"))) for x in entries]
        doms = [d for d in doms if d is not None]
        rows.append({
            "month": month,
            "closedCount": len(entries),
            "medianSalePrice": _safe_stat(sale_prices, statistics.median),
            "medianSalePpsf": _safe_stat(ppsfs, statistics.median),
            "avgDom": _safe_stat(doms, statistics.mean),
        })

    yoy_price = None
    yoy_ppsf = None
    if len(rows) >= 2:
        latest = rows[-1]
        year_ago = next((r for r in rows if r["month"] <= _shift_month(latest["month"], -12)), None)
        if year_ago:
            if latest["medianSalePrice"] and year_ago["medianSalePrice"]:
                yoy_price = round((latest["medianSalePrice"] - year_ago["medianSalePrice"]) / year_ago["medianSalePrice"], 4)
            if latest["medianSalePpsf"] and year_ago["medianSalePpsf"]:
                yoy_ppsf = round((latest["medianSalePpsf"] - year_ago["medianSalePpsf"]) / year_ago["medianSalePpsf"], 4)

    return {
        "monthly": rows,
        "yoyMedianPriceChangePct": yoy_price,
        "yoyMedianPpsfChangePct": yoy_ppsf,
    }


def _shift_month(month_str: str, delta_months: int) -> str:
    year, month = int(month_str[:4]), int(month_str[5:7])
    total = year * 12 + (month - 1) + delta_months
    return f"{total // 12:04d}-{(total % 12) + 1:02d}"


def build_top_closed_comps(closed: list[dict]) -> list[str]:
    """Top TOP_COMPS closed listingKeys — ranked by (completeness, recency)."""
    completeness = ("closePrice", "livingArea", "bedsTotal", "bathsTotal", "closeDate")
    scored = []
    for l in closed:
        score = sum(1 for f in completeness if l.get(f) not in (None, "", 0))
        cd = coerce_date(l.get("closeDate")) or datetime(1900, 1, 1, tzinfo=timezone.utc)
        scored.append((score, cd, l))
    scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [l.get("listingKey") for _, _, l in scored[:TOP_COMPS] if l.get("listingKey")]


def build_top_active_comps(actives: list[dict]) -> list[str]:
    """Top TOP_COMPS active listingKeys — ranked by (completeness, price-descending)."""
    completeness = ("listPrice", "livingArea", "bedsTotal", "bathsTotal", "listingContractDate")
    scored = []
    for l in actives:
        score = sum(1 for f in completeness if l.get(f) not in (None, "", 0))
        price = l.get("listPrice") or 0
        scored.append((score, price, l))
    scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [l.get("listingKey") for _, _, l in scored[:TOP_COMPS] if l.get("listingKey")]


def build_quality(active_count: int, closed_in_window: int, closed_total: int) -> dict:
    notes = []
    if active_count < INSUFFICIENT_ACTIVE:
        notes.append(f"Fewer than {INSUFFICIENT_ACTIVE} active listings.")
    if closed_in_window < INSUFFICIENT_CLOSED_IN_WINDOW:
        notes.append(f"Fewer than {INSUFFICIENT_CLOSED_IN_WINDOW} closings in last {SAMPLE_WINDOW_MONTHS} months.")

    if closed_in_window < INSUFFICIENT_CLOSED_IN_WINDOW and active_count < INSUFFICIENT_ACTIVE:
        confidence = "insufficient"
    elif closed_total >= CLOSING_CAP and active_count >= INSUFFICIENT_ACTIVE * 2:
        confidence = "good"
    else:
        confidence = "fair"

    return {"confidence": confidence, "notes": notes}

# ──────────────────────────────────────────────────────────────────────────────
# 🔎 PER-SUBDIVISION BUILDER
# ──────────────────────────────────────────────────────────────────────────────


def compute_absorption_rate(active_count: int, closed_in_window_count: int,
                             window_months: int) -> float | None:
    """
    Absorption rate = closed-per-month / active-count.
    Higher number = faster market (more homes absorbed per unit of active supply).
    Returns None when either side is zero.
    """
    if active_count <= 0 or window_months <= 0 or closed_in_window_count <= 0:
        return None
    closed_per_month = closed_in_window_count / window_months
    return round(closed_per_month / active_count, 4)


def compute_cma_stats(sub: dict) -> dict | None:
    """Compute cmaStats payload. Returns None when the subdivision has zero listings (skip)."""
    name = sub.get("name") or ""
    city = sub.get("city") or ""

    name_regex = {"$regex": f"^{_escape_regex(name)}$", "$options": "i"}

    # Only truly on-market statuses. Pending/AUC now live in unified_in_escrow
    # (not used for CMA per user call); Hold lives in unified_off_market.
    active_query = {
        "subdivisionName": name_regex,
        "city": city,
        "standardStatus": {"$in": ["Active", "ComingSoon"]},
    }
    closed_query = {
        "subdivisionName": name_regex,
        "city": city,
    }

    actives = [l for l in listings_coll.find(active_query) if not is_lease(l)]
    closed_all = [l for l in closed_coll.find(closed_query) if not is_lease(l)]

    # Skip entirely if nothing to compute (per spec: "skip subdivisions with 0 listings")
    if not actives and not closed_all:
        return None

    closed_all_sorted = sorted(
        closed_all,
        key=lambda l: coerce_date(l.get("closeDate")) or datetime(1900, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    closed_sample = closed_all_sorted[:CLOSING_CAP]

    window_start = datetime.now(timezone.utc) - timedelta(days=SAMPLE_WINDOW_MONTHS * 30)
    window_end = datetime.now(timezone.utc)
    closed_in_window = [
        l for l in closed_all_sorted
        if coerce_date(l.get("closeDate")) and coerce_date(l.get("closeDate")) >= window_start
    ]

    quality = build_quality(len(actives), len(closed_in_window), len(closed_sample))
    last_updated = window_end.isoformat()
    sample_window = {
        "months": SAMPLE_WINDOW_MONTHS,
        "startDate": window_start.isoformat(),
        "endDate": window_end.isoformat(),
        "listingCap": CLOSING_CAP,
    }

    # Insufficient data: still write a minimal record with the spec-required keys
    # (empty arrays, null numerics) so frontend can distinguish "limited data"
    # from "never computed" without 500-ing on missing fields.
    if quality["confidence"] == "insufficient":
        return {
            "lastUpdated": last_updated,
            "active": {"count": len(actives)},
            "closed": {"count": len(closed_sample)},
            "absorptionRate": None,
            "topActiveComps": [],
            "topClosedComps": [],
            "subdivisionProfile": {
                "poolPrevalence": None, "spaPrevalence": None, "garagePrevalence": None,
                "gatedCommunity": None, "seniorCommunity": None,
            },
            "quality": quality,
            "sampleWindow": sample_window,
        }

    return {
        # Spec-named fields
        "lastUpdated": last_updated,
        "active": build_active_block(actives),
        "closed": build_closed_block(closed_sample),
        "absorptionRate": compute_absorption_rate(len(actives), len(closed_in_window), SAMPLE_WINDOW_MONTHS),
        "topActiveComps": build_top_active_comps(actives),
        "topClosedComps": build_top_closed_comps(closed_sample),
        "subdivisionProfile": build_subdivision_profile(actives, closed_sample),
        # Extras (useful for future frontend features; safe to ignore)
        "quality": quality,
        "sampleWindow": sample_window,
        "bySubType": build_by_subtype(actives, closed_sample),
        "trends": build_trends(closed_in_window),
    }


def _escape_regex(s: str) -> str:
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Build cmaStats on every subdivision.")
    parser.add_argument("--all", action="store_true", help="Process every subdivision.")
    parser.add_argument("--subdivision", dest="subdivision_name",
                        help="Process a single subdivision by display name (case-insensitive exact match).")
    parser.add_argument("--slug", help="Process a single subdivision by slug (alias for --subdivision).")
    parser.add_argument("--city", help="Process all subdivisions in this city.")
    parser.add_argument("--county", help="Process all subdivisions in this county.")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write.")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-subdivision logging.")
    args = parser.parse_args()

    if not (args.slug or args.subdivision_name or args.city or args.county or args.all):
        print("[ERROR] Must pass --all, --subdivision <name>, --slug <slug>, --city <city>, or --county <county>.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    query: dict = {}
    if args.slug:
        query["slug"] = args.slug
    elif args.subdivision_name:
        query["name"] = {"$regex": f"^{_escape_regex(args.subdivision_name)}$", "$options": "i"}
    elif args.city:
        query["city"] = args.city
    elif args.county:
        query["county"] = {"$regex": f"^{_escape_regex(args.county)}$", "$options": "i"}

    cursor = subs_coll.find(query)
    if args.limit:
        cursor = cursor.limit(args.limit)
    subs = list(cursor)

    if not subs:
        print("❌ No subdivisions matched the query.")
        return

    print(f"🏘️  {len(subs):,} subdivisions to process  (dry-run={args.dry_run})")

    started = time.time()
    pending: list[UpdateOne] = []
    written = 0
    insufficient = 0
    skipped_empty = 0
    processed = 0
    failed = 0

    for sub in subs:
        processed += 1
        label = f"{sub.get('city','?')} / {sub.get('name','?')}"
        try:
            stats = compute_cma_stats(sub)
        except Exception as e:
            failed += 1
            print(f"  [ERR] {label}: {e}")
            continue

        if stats is None:
            skipped_empty += 1
            if args.verbose:
                print(f"  [{processed:>4}/{len(subs)}] {label[:60]:<60} SKIP (no listings)")
            continue

        conf = stats.get("quality", {}).get("confidence", "unknown")
        if conf == "insufficient":
            insufficient += 1

        active_count = stats.get("active", {}).get("count", 0)
        closed_count = stats.get("closed", {}).get("count", 0)
        median = stats.get("closed", {}).get("medianClosePrice")
        median_str = f"median ${median:,.0f}" if median else "median —"

        # Spec log format: "Processing 1/1424: PGA West... 45 active, 120 closed, median $650k"
        print(
            f"Processing {processed}/{len(subs)}: {label[:45]:<45} "
            f"{active_count:>3} active, {closed_count:>3} closed, {median_str}"
        )
        if args.verbose:
            print(f"      absorption={stats.get('absorptionRate')}  confidence={conf}  "
                  f"topActive={len(stats.get('topActiveComps',[]))}  "
                  f"topClosed={len(stats.get('topClosedComps',[]))}")

        if args.dry_run and processed <= 1:
            print(json.dumps(stats, indent=2, default=str))

        if not args.dry_run:
            pending.append(UpdateOne({"_id": sub["_id"]}, {"$set": {"cmaStats": stats}}))
            if len(pending) >= BULK_BATCH:
                res = subs_coll.bulk_write(pending, ordered=False)
                written += res.modified_count
                pending.clear()

    if not args.dry_run and pending:
        res = subs_coll.bulk_write(pending, ordered=False)
        written += res.modified_count

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    print(f"Processed:       {processed:,}")
    print(f"Skipped (empty): {skipped_empty:,}")
    print(f"Insufficient:    {insufficient:,}")
    print(f"Failed:          {failed:,}")
    print(f"Written:         {written:,}" if not args.dry_run else "Dry-run (no writes)")
    print(f"Elapsed:         {elapsed/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
