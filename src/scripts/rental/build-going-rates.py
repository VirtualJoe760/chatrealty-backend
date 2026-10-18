#!/usr/bin/env python3
"""
Build Rent Rates (area-level "going rate" for rentals)

The rental analog of build-subdivision-cma.py. For every subdivision AND every
ZIP ("area"), reads what has actually RENTED — closed Residential-Lease listings
(propertyType "B") from unified_closed_listings, where closePrice == the monthly
rent the unit leased at — plus the current for-rent supply from unified_listings,
and computes a `rentStats` block that establishes a defensible "going rate".

Two write targets (scope decision: subdivision + ZIP, session 2026-06-09):
  • subdivisions.rentStats  — mirrors cmaStats; per-community going rate.
  • rent_rates collection    — one doc per ZIP; ~100% postalCode coverage closes
                               the ~43% subdivision-orphan gap so an "area" rent
                               lookup works everywhere, even where a listing's
                               subdivisionName doesn't match any subdivision doc.

Both targets carry the same `rentStats` shape; only `scope`/`geo` differ.

── The seasonal trap ─────────────────────────────────────────────────────────
Coachella Valley leases mix annual long-term rentals with furnished monthly
peak-season vacation rents. There is NO frequency/term field in the data
(leaseAmountFrequency / leaseTerm are empty), so closePrice alone can't tell a
$3,000/mo annual lease from a $12,000/mo January vacation rental. A naive median
blends them into a meaningless number.

Handling:
  • Segment by `furnished` (Furnished / Unfurnished / Partial / Unknown).
  • Unfurnished is clean annual rent — it's the headline "going rate" basis,
    because that's what a buy-and-hold investor underwrites for cash flow.
  • When the unfurnished sample is too thin, fall back to a TRIMMED median over
    all leases (10th–90th percentile) which strips the seasonal long tail.
  • Furnished stats are still emitted (labeled "may include seasonal") so the
    STR / seasonal use case isn't lost.

Usage:
    # One subdivision, no DB writes
    python3 src/scripts/cma/build-rent-rates.py --subdivision "La Quinta Cove" --dry-run

    # One city (subdivisions in it + its ZIPs)
    python3 src/scripts/cma/build-rent-rates.py --city "Palm Desert"

    # ZIPs only / subdivisions only
    python3 src/scripts/cma/build-rent-rates.py --all --zips-only
    python3 src/scripts/cma/build-rent-rates.py --all --subs-only

    # Full run (cron)
    python3 src/scripts/cma/build-rent-rates.py --all
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
SAMPLE_WINDOW_MONTHS = 24           # rentals need more lookback for volume than sales
RECENT_TREND_MONTHS = 12            # window used for YoY rent trend
INSUFFICIENT_RENTED = 5             # < this many rented comps in window ⇒ insufficient
UNFURNISHED_FLOOR = 8               # unfurnished sample below this ⇒ use trimmed-all basis
TRIM_LOW, TRIM_HIGH = 0.10, 0.90    # percentile trim that strips the seasonal long tail
TOP_COMPS = 5
BULK_BATCH = 200
MAX_BEDS_BUCKET = 5                 # 5 means "5+"

LEASE_MATCH = {"$or": [{"propertyType": "B"}, {"propertyTypeName": "Residential Lease"}]}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
subs_coll = db.subdivisions
listings_coll = db.unified_listings          # active for-rent supply
closed_coll = db.unified_closed_listings      # rented (closed leases)
rent_rates_coll = db.rent_rates               # ZIP-level output
rent_rates_coll.create_index("postalCode", unique=True)  # idempotent; backs the upsert
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS  (shared idioms with build-subdivision-cma.py)
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


def _escape_regex(s: str) -> str:
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)


def _safe_stat(values, fn):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    try:
        return round(fn(vals), 2)
    except statistics.StatisticsError:
        return None


def _percentile(values, p):
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


def _trimmed(values, lo=TRIM_LOW, hi=TRIM_HIGH):
    """Drop the lowest `lo` and highest `1-hi` fraction — strips seasonal spikes."""
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    if len(vals) < 5:
        return vals
    a = int(len(vals) * lo)
    b = max(a + 1, int(round(len(vals) * hi)))
    return vals[a:b]


def _rpsf(rent, sqft):
    if not rent or not sqft or sqft <= 0:
        return None
    return rent / sqft


def _month_bucket(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def furnished_class(listing: dict) -> str:
    """Normalize the sparse `furnished` field to one of our 4 buckets."""
    raw = (listing.get("furnished") or "").strip().lower()
    if not raw:
        return "unknown"
    if raw.startswith("unfurn"):
        return "unfurnished"
    if raw.startswith("partial"):
        return "partial"
    if raw.startswith("furn"):
        return "furnished"
    return "unknown"


def beds_bucket(listing: dict):
    b = listing.get("bedsTotal")
    if b is None:
        b = listing.get("bedroomsTotal")
    if not isinstance(b, (int, float)):
        return None
    b = int(b)
    if b < 0:
        return None
    return min(b, MAX_BEDS_BUCKET)


def listing_rent(listing: dict):
    """Monthly rent a closed lease leased at = closePrice (fallback currentPrice/listPrice)."""
    for f in ("closePrice", "currentPrice", "listPrice"):
        v = listing.get(f)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None

# ──────────────────────────────────────────────────────────────────────────────
# 🧮 STAT BLOCKS
# ──────────────────────────────────────────────────────────────────────────────


def rent_block(leases: list[dict]) -> dict:
    """Distribution summary for a set of rented (or asking) leases."""
    if not leases:
        return {"count": 0}
    rents = [listing_rent(l) for l in leases]
    rents = [r for r in rents if r]
    rpsfs = [_rpsf(listing_rent(l), l.get("livingArea")) for l in leases]
    rpsfs = [r for r in rpsfs if r]
    sqfts = [l.get("livingArea") for l in leases if l.get("livingArea")]
    return {
        "count": len(leases),
        "medianRent": _safe_stat(rents, statistics.median),
        "avgRent": _safe_stat(rents, statistics.mean),
        "minRent": min(rents) if rents else None,
        "maxRent": max(rents) if rents else None,
        "p25Rent": _percentile(rents, 0.25),
        "p75Rent": _percentile(rents, 0.75),
        "medianRentPerSqft": _safe_stat(rpsfs, statistics.median),
        "medianSqft": _safe_stat(sqfts, statistics.median),
    }


def by_bedroom(leases: list[dict]) -> list[dict]:
    groups: dict[int, list] = defaultdict(list)
    for l in leases:
        b = beds_bucket(l)
        if b is not None:
            groups[b].append(l)
    out = []
    for beds in sorted(groups):
        g = groups[beds]
        rents = [listing_rent(l) for l in g]
        rents = [r for r in rents if r]
        rpsfs = [_rpsf(listing_rent(l), l.get("livingArea")) for l in g]
        rpsfs = [r for r in rpsfs if r]
        out.append({
            "beds": beds,
            "bedsLabel": f"{beds}+" if beds >= MAX_BEDS_BUCKET else str(beds),
            "count": len(g),
            "medianRent": _safe_stat(rents, statistics.median),
            "p25Rent": _percentile(rents, 0.25),
            "p75Rent": _percentile(rents, 0.75),
            "medianRentPerSqft": _safe_stat(rpsfs, statistics.median),
        })
    return out


def going_rate(unfurnished: list[dict], all_rented: list[dict]) -> dict:
    """
    The headline number. Prefer the unfurnished (clean annual) median. When the
    unfurnished sample is thin, fall back to a trimmed median over ALL rented
    leases (10th–90th pctile) which strips the seasonal long tail.
    """
    unf_rents = [listing_rent(l) for l in unfurnished]
    unf_rents = [r for r in unf_rents if r]
    if len(unf_rents) >= UNFURNISHED_FLOOR:
        basis, sample = "unfurnished-median", unf_rents
        rpsf_src = unfurnished
    else:
        all_rents = [listing_rent(l) for l in all_rented]
        all_rents = [r for r in all_rents if r]
        sample = _trimmed(all_rents)
        basis = "trimmed-all-median"
        rpsf_src = all_rented
    rpsfs = [_rpsf(listing_rent(l), l.get("livingArea")) for l in rpsf_src]
    rpsfs = [r for r in rpsfs if r]
    return {
        "monthlyRent": _safe_stat(sample, statistics.median),
        "annualRent": round(_safe_stat(sample, statistics.median) * 12, 2) if sample else None,
        "rentPerSqft": _safe_stat(_trimmed(rpsfs), statistics.median),
        "basis": basis,                 # which method produced monthlyRent
        "sampleSize": len(sample),
    }


def rent_trends(rented_in_trend_window: list[dict]) -> dict:
    monthly: dict[str, list] = defaultdict(list)
    for l in rented_in_trend_window:
        d = coerce_date(l.get("closeDate"))
        r = listing_rent(l)
        if d and r:
            monthly[_month_bucket(d)].append(r)
    series = [{"month": m, "count": len(v), "medianRent": _safe_stat(v, statistics.median)}
              for m, v in sorted(monthly.items())]

    # YoY: median rent of the most recent 3 months vs the same 3 months a year ago.
    yoy = None
    if len(series) >= 13:
        recent = [s["medianRent"] for s in series[-3:] if s["medianRent"]]
        prior = [s["medianRent"] for s in series[-15:-12] if s["medianRent"]]
        if recent and prior:
            r, p = statistics.mean(recent), statistics.mean(prior)
            if p > 0:
                yoy = round((r - p) / p * 100, 2)
    return {"monthly": series, "yoyMedianRentChangePct": yoy}


def top_rented_comps(rented_sorted: list[dict]) -> list[str]:
    out = []
    for l in rented_sorted:
        k = l.get("listingKey")
        if k:
            out.append(k)
        if len(out) >= TOP_COMPS:
            break
    return out


def build_quality(rented_in_window: int, active_count: int, unfurnished_in_window: int) -> dict:
    notes = []
    if rented_in_window < INSUFFICIENT_RENTED:
        notes.append(f"Only {rented_in_window} rented comp(s) in the {SAMPLE_WINDOW_MONTHS}-month window.")
        return {"confidence": "insufficient", "notes": notes}
    if unfurnished_in_window < UNFURNISHED_FLOOR:
        notes.append("Thin unfurnished sample — going rate uses a trimmed all-lease median "
                     "(furnished/seasonal rents trimmed at the 10th–90th percentile).")
    if rented_in_window >= 25 and unfurnished_in_window >= UNFURNISHED_FLOOR:
        conf = "good"
    elif rented_in_window >= 12:
        conf = "fair"
    else:
        conf = "fair" if rented_in_window >= INSUFFICIENT_RENTED else "insufficient"
    return {"confidence": conf, "notes": notes}

# ──────────────────────────────────────────────────────────────────────────────
# 🔎 CORE COMPUTE  (pure: from pre-grouped in-memory lists, no per-geo queries)
# ──────────────────────────────────────────────────────────────────────────────

# Only the fields the stat blocks touch — keeps the windowed scan light.
_LEASE_FIELDS = ["closePrice", "currentPrice", "listPrice", "livingArea", "bedsTotal",
                 "bedroomsTotal", "furnished", "closeDate", "listingKey",
                 "subdivisionName", "city", "postalCode", "standardStatus"]
LEASE_PROJECTION = {f: 1 for f in _LEASE_FIELDS}


def assemble_stats(scope: str, geo: dict, rented_in_window: list[dict], actives: list[dict]) -> dict | None:
    """Build a rentStats block from leases already filtered to the window + geo."""
    if not rented_in_window and not actives:
        return None

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=SAMPLE_WINDOW_MONTHS * 30)
    rented_sorted = sorted(
        rented_in_window,
        key=lambda l: coerce_date(l.get("closeDate")) or datetime(1900, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )

    by_furn = defaultdict(list)
    for l in rented_in_window:
        by_furn[furnished_class(l)].append(l)
    unfurnished = by_furn["unfurnished"]
    furnished = by_furn["furnished"]

    quality = build_quality(len(rented_in_window), len(actives), len(unfurnished))
    base = {
        "lastUpdated": now.isoformat(),
        "scope": scope,
        "geo": geo,
        "sampleWindow": {"months": SAMPLE_WINDOW_MONTHS,
                         "startDate": window_start.isoformat(),
                         "endDate": now.isoformat()},
        "quality": quality,
    }

    if quality["confidence"] == "insufficient":
        base.update({
            "goingRate": {"monthlyRent": None, "annualRent": None, "rentPerSqft": None,
                          "basis": "insufficient", "sampleSize": len(rented_in_window)},
            "rented": {"count": len(rented_in_window)},
            "unfurnished": {"count": len(unfurnished)},
            "furnished": {"count": len(furnished)},
            "active": {"count": len(actives)},
            "byBedroom": [],
            "topRentedComps": [],
        })
        return base

    base.update({
        "goingRate": going_rate(unfurnished, rented_in_window),
        "rented": rent_block(rented_in_window),                 # all rented, in window
        "unfurnished": rent_block(unfurnished),                 # clean annual basis
        "furnished": {**rent_block(furnished),
                      "note": "may include monthly/seasonal vacation rents"},
        "active": rent_block(actives),                          # current asking-rent supply
        "byBedroom": by_bedroom(rented_in_window),
        "trends": rent_trends(rented_sorted),
        "topRentedComps": top_rented_comps(rented_sorted),
    })
    return base

# ──────────────────────────────────────────────────────────────────────────────
# 🗂️  WINDOWED LOAD + IN-MEMORY GROUPING
# ──────────────────────────────────────────────────────────────────────────────


def load_window(scope_match: dict | None):
    """
    One indexed scan each for rented + active leases inside the sample window,
    instead of two queries per geo. `closeDate` is indexed (the range drives the
    plan AND excludes the dirty year-5015 future dates); `unified_closed_listings`
    has NO postalCode index, so a per-ZIP query would collscan 1.5M docs — this
    avoids that entirely.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=SAMPLE_WINDOW_MONTHS * 30)
    date_ceiling = now + timedelta(days=2)

    cq = {"closeDate": {"$gte": window_start, "$lte": date_ceiling}, **LEASE_MATCH}
    aq = {"standardStatus": {"$in": ["Active", "ComingSoon"]}, **LEASE_MATCH}
    if scope_match:
        cq.update(scope_match)
        aq.update(scope_match)

    rented = list(closed_coll.find(cq, LEASE_PROJECTION))
    actives = list(listings_coll.find(aq, LEASE_PROJECTION))
    return rented, actives


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


def dominant_city(leases: list[dict]) -> str | None:
    cities = [l.get("city") for l in leases if l.get("city")]
    return Counter(cities).most_common(1)[0][0] if cities else None

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Build rentStats (going rate) on subdivisions + ZIPs.")
    parser.add_argument("--all", action="store_true", help="Process everything.")
    parser.add_argument("--subdivision", dest="subdivision_name", help="One subdivision by display name.")
    parser.add_argument("--slug", help="One subdivision by slug.")
    parser.add_argument("--city", help="All subdivisions + ZIPs in this city.")
    parser.add_argument("--county", help="All subdivisions in this county.")
    parser.add_argument("--zip", dest="postal", help="One ZIP.")
    parser.add_argument("--subs-only", action="store_true", help="Subdivisions only, skip ZIPs.")
    parser.add_argument("--zips-only", action="store_true", help="ZIPs only, skip subdivisions.")
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

    # Scope the windowed scan to a city when we can — shrinks the pull.
    scope_match = {"city": args.city} if args.city else None
    print("📥 Loading windowed lease set…")
    rented, actives = load_window(scope_match)
    print(f"   {len(rented):,} rented (closed) + {len(actives):,} active leases in the "
          f"{SAMPLE_WINDOW_MONTHS}-month window")
    sub_rented, sub_active = group_by_sub(rented), group_by_sub(actives)
    zip_rented, zip_active = group_by_zip(rented), group_by_zip(actives)

    # ── Subdivisions ──────────────────────────────────────────────────────────
    sub_written = sub_skipped = sub_insufficient = sub_failed = 0
    if do_subs:
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
                stats = assemble_stats("subdivision", geo, sub_rented.get(key, []), sub_active.get(key, []))
            except Exception as e:
                sub_failed += 1
                print(f"  [ERR] {label}: {e}")
                continue
            if stats is None:
                sub_skipped += 1
                continue
            conf = stats["quality"]["confidence"]
            if conf == "insufficient":
                sub_insufficient += 1
            gr = stats["goingRate"].get("monthlyRent")
            gr_str = f"${gr:,.0f}/mo" if gr else "—"
            if args.verbose or conf != "insufficient":
                print(f"Processing sub {i}/{len(subs)}: {label[:42]:<42} "
                      f"{stats.get('rented',{}).get('count',0):>3} rented  going {gr_str:>10}  [{conf}]")
            if args.dry_run and i <= 1:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"_id": sub["_id"]}, {"$set": {"rentStats": stats}}))
                if len(pending) >= BULK_BATCH:
                    sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count
                    pending.clear()
        if not args.dry_run and pending:
            sub_written += subs_coll.bulk_write(pending, ordered=False).modified_count

    # ── ZIPs ──────────────────────────────────────────────────────────────────
    zip_written = zip_skipped = zip_insufficient = zip_failed = 0
    if do_zips:
        if args.postal:
            zip_keys = [args.postal]
        else:
            zip_keys = sorted(zip_rented.keys(), key=lambda z: -len(zip_rented[z]))
        if args.limit:
            zip_keys = zip_keys[:args.limit]
        print(f"🗺️  {len(zip_keys):,} ZIPs to process  (dry-run={args.dry_run})")

        pending: list[UpdateOne] = []
        for i, z in enumerate(zip_keys, 1):
            r_list = zip_rented.get(z, [])
            geo = {"postalCode": z, "city": dominant_city(r_list)}
            try:
                stats = assemble_stats("zip", geo, r_list, zip_active.get(z, []))
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
            gr = stats["goingRate"].get("monthlyRent")
            gr_str = f"${gr:,.0f}/mo" if gr else "—"
            if args.verbose or conf != "insufficient":
                print(f"Processing zip {i}/{len(zip_keys)}: {str(z):<8} {str(geo['city'])[:22]:<22} "
                      f"{stats.get('rented',{}).get('count',0):>4} rented  going {gr_str:>10}  [{conf}]")
            if args.dry_run and i <= 1:
                print(json.dumps(stats, indent=2, default=str))
            if not args.dry_run:
                pending.append(UpdateOne({"postalCode": z}, {"$set": stats}, upsert=True))
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
