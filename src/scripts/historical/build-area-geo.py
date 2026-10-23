#!/usr/bin/env python3
"""
Build Area Geometry (where each area IS, so sales volume can be mapped)

`market_history` says how much each area sold. It does not say where the area
is, so nothing in it can be drawn on a map. This script supplies the missing
half: one stored centroid, bounding box and radius per area, keyed exactly the
way `market_history` keys the same area, so a heat map is a join and not a
computation.

Nothing is geocoded and nothing is fetched. The closings already carry
`latitude`/`longitude` — 94% of Coachella Valley closings that have a
subdivision name, and 0 of them land outside California. This only aggregates
what the feed already gave us.

── Why latitude/longitude and not `coordinates` ─────────────────────────────
Both exist. The GeoJSON `coordinates` field is present on 82.8% of named CV
closings while raw `latitude`/`longitude` sit at 94.0% — older documents were
written before flatten.py started building the GeoJSON point. So the raw pair
is the better source and the point is rebuilt here.

── Why the centroid is trustworthy, and when it is not ──────────────────────
A real subdivision is a tight cluster: Sun City's 90th-percentile radius is
1.18 mi, Sun City Shadow Hills 0.93, La Quinta Cove 0.86. A centroid describes
those well.

It describes a placeholder name terribly. "Not Applicable" in Thermal spans a
23-mile radius because it is not a place, it is an empty field — and every one
of the 14 CV areas whose p90 radius exceeds 3 miles is a placeholder. They are
stored with `placeholder: true` and a real `radiusMiles` so a consumer can
filter them, and the GeoJSON export drops them by default. Check `radiusMiles`
before trusting any centroid.

The centroid is the MEDIAN latitude and longitude, not the mean: a handful of
mis-geocoded points (Desert Princess has one 9.9 mi out) would drag a mean and
cannot move a median.

Geometry is pooled across property types and years. A subdivision does not move
between 2022 and 2026, or between a sale and a lease, so splitting the points
would only thin them.

Usage:
    # one metro quickly, then the whole feed
    python3 src/scripts/historical/build-area-geo.py --cities "Palm Desert,Indio"
    python3 src/scripts/historical/build-area-geo.py --all

    # what is stored now
    python3 src/scripts/historical/build-area-geo.py --status

    # map-ready export: centroids joined to market_history sales counts
    python3 src/scripts/historical/build-area-geo.py --geojson /tmp/cv.geojson \\
        --cv --year 2026
"""

import os
import re
import sys
import json
import time
import math
import argparse
import statistics
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter

from pymongo import MongoClient, UpdateOne, ASCENDING, GEOSPHERE
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
SCOPES = ("zip", "city", "subdivision")
MIN_POINTS = 5            # matches MIN_SAMPLE in build-market-history.py
BULK_BATCH = 500

# Generous California envelope. Nothing in the CV sample fell outside it, so it
# is a guard against feed noise (0,0 and transposed pairs), not a real filter.
LAT_RANGE = (32.3, 42.1)
LNG_RANGE = (-124.6, -114.0)

# Mirrors rank-subdivisions.py, which mirrors build-listing-cma.py and three
# others. A placeholder name is not a place: its points scatter across a county.
PLACEHOLDER_SUBDIVISIONS = {
    "not applicable", "n/a", "none", "other", "na", "no hoa", "see remarks",
    "unknown", "tbd", "various", "not in a development", "no subdivision",
    "-", "--", ".", "",
}
PLACEHOLDER_RE = re.compile(r"^(?:.+:\s*)?other(?:\s*-\s*\d+)?$")

COACHELLA_VALLEY = [
    "Palm Springs", "Palm Desert", "La Quinta", "Indian Wells",
    "Rancho Mirage", "Cathedral City", "Indio", "Desert Hot Springs",
    "Coachella", "Thermal", "Bermuda Dunes", "Mecca", "Thousand Palms",
    "Sky Valley",
]

PROJECTION = {"city": 1, "postalCode": 1, "subdivisionName": 1,
              "latitude": 1, "longitude": 1}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
closed_coll = db.unified_closed_listings
history_coll = db.market_history
geo_coll = db.area_geo
print("✅ Connected to MongoDB")


def ensure_indexes():
    geo_coll.create_index([("centroid", GEOSPHERE)], name="centroid_2dsphere")
    geo_coll.create_index([("scope", ASCENDING), ("key", ASCENDING)], name="scope_key")
    geo_coll.create_index([("city", ASCENDING)], name="city")


# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def is_placeholder(label):
    if not isinstance(label, str):
        return True
    norm = label.strip().lower()
    return norm in PLACEHOLDER_SUBDIVISIONS or bool(PLACEHOLDER_RE.match(norm))


def area_keys(doc):
    """MUST stay identical to area_keys() in build-market-history.py. The whole
    point of this collection is that its `_id` joins that one's; a drift here
    silently produces a map with holes in it."""
    out = []
    zp = (doc.get("postalCode") or "").strip()
    city = (doc.get("city") or "").strip()
    sub = (doc.get("subdivisionName") or "").strip()
    if zp:
        out.append(("zip", zp, city or None))
    if city:
        out.append(("city", city.lower(), city))
    if city and sub:
        out.append(("subdivision", f"{city.lower()}|{sub.lower()}", sub))
    return out


def miles(a, b):
    """Haversine. Only ever called on points inside one metro, so the spherical
    approximation is far below the precision of the underlying geocode."""
    (la1, ln1), (la2, ln2) = a, b
    p = math.pi / 180
    h = (math.sin((la2 - la1) * p / 2) ** 2
         + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((ln2 - ln1) * p / 2) ** 2)
    return 7917.5 * math.asin(min(1.0, math.sqrt(h)))


def _pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


# ──────────────────────────────────────────────────────────────────────────────
# 📍 COLLECT
# ──────────────────────────────────────────────────────────────────────────────

def collect(cities=None, verbose=False):
    """One bucket of points per (scope, key), over every year and property type."""
    q = {"latitude": {"$ne": None}, "longitude": {"$ne": None}}
    if cities:
        q["city"] = {"$in": cities}

    buckets = defaultdict(lambda: {"pts": [], "labels": Counter(), "cities": Counter()})
    scanned = kept = dropped = 0
    for d in closed_coll.find(q, PROJECTION):
        scanned += 1
        if verbose and scanned % 200_000 == 0:
            print(f"    … {scanned:,} scanned")
        la, ln = d.get("latitude"), d.get("longitude")
        if not isinstance(la, (int, float)) or not isinstance(ln, (int, float)):
            dropped += 1
            continue
        if not (LAT_RANGE[0] <= la <= LAT_RANGE[1]
                and LNG_RANGE[0] <= ln <= LNG_RANGE[1]):
            dropped += 1
            continue
        kept += 1
        for scope, key, label in area_keys(d):
            b = buckets[(scope, key)]
            b["pts"].append((la, ln))
            if label:
                b["labels"][label] += 1
            if d.get("city"):
                b["cities"][d["city"].strip()] += 1
    print(f"  scanned {scanned:,} closings, used {kept:,}, "
          f"dropped {dropped:,} for bad or out-of-range coordinates")
    return buckets


def build_docs(buckets, now):
    docs = []
    for (scope, key), b in buckets.items():
        pts = b["pts"]
        if len(pts) < MIN_POINTS:
            continue
        clat = statistics.median(p[0] for p in pts)
        clng = statistics.median(p[1] for p in pts)
        dists = sorted(miles((clat, clng), p) for p in pts)
        label = b["labels"].most_common(1)[0][0] if b["labels"] else None
        city = b["cities"].most_common(1)[0][0] if b["cities"] else None
        docs.append({
            "_id": f"{scope}:{key}",
            "scope": scope,
            "key": key,
            "label": label,
            "city": city,
            # GeoJSON order is [lng, lat]. Getting this backwards puts the
            # Coachella Valley in the Indian Ocean and the 2dsphere index will
            # accept it without complaint.
            "centroid": {"type": "Point", "coordinates": [clng, clat]},
            "lat": round(clat, 6),
            "lng": round(clng, 6),
            "bbox": {
                "minLat": round(min(p[0] for p in pts), 6),
                "maxLat": round(max(p[0] for p in pts), 6),
                "minLng": round(min(p[1] for p in pts), 6),
                "maxLng": round(max(p[1] for p in pts), 6),
            },
            # p90, not max: one bad geocode should not describe the area.
            "radiusMiles": round(_pct(dists, 0.90) or 0.0, 3),
            "medianRadiusMiles": round(statistics.median(dists), 3),
            "maxRadiusMiles": round(dists[-1], 3),
            "pointCount": len(pts),
            "placeholder": scope == "subdivision" and is_placeholder(label),
            "source": "unified_closed_listings latitude/longitude",
            "builtAt": now,
        })
    return docs


def write_docs(docs, dry_run):
    if dry_run or not docs:
        return 0
    written = 0
    for i in range(0, len(docs), BULK_BATCH):
        chunk = docs[i:i + BULK_BATCH]
        res = geo_coll.bulk_write(
            [UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in chunk],
            ordered=False)
        written += res.upserted_count + res.modified_count
    return written


# ──────────────────────────────────────────────────────────────────────────────
# 🗺️ EXPORT
# ──────────────────────────────────────────────────────────────────────────────

def export_geojson(path, scope, year, ptype, cities, min_sales,
                   keep_placeholders):
    """Join stored geometry to stored sales counts. Both halves are already
    computed; this only puts them in one file a map can read."""
    hq = {"scope": scope, "propertyType": ptype}
    if year:
        hq["year"] = year
    sales, meta = defaultdict(int), {}
    for h in history_coll.find(hq):
        sales[h["key"]] += h.get("closedCount", 0)
        if h["key"] not in meta or h["year"] >= meta[h["key"]]["year"]:
            meta[h["key"]] = h

    wanted = {c.strip().lower() for c in cities} if cities else None
    feats, missing = [], 0
    for g in geo_coll.find({"scope": scope}):
        key = g["key"]
        if key not in sales:
            continue
        if not keep_placeholders and g.get("placeholder"):
            continue
        if wanted:
            ck = key.split("|")[0] if scope == "subdivision" else (g.get("city") or "").lower()
            if ck not in wanted:
                continue
        n = sales[key]
        if n < min_sales:
            continue
        m = meta[key]
        feats.append({
            "type": "Feature",
            "geometry": g["centroid"],
            "properties": {
                "key": key,
                "label": g.get("label"),
                "city": g.get("city"),
                "sales": n,
                "year": m["year"] if year else f"{scope} total",
                "medianClosePrice": m.get("medianClosePrice"),
                "medianPricePerSqft": m.get("medianPricePerSqft"),
                "medianDaysOnMarket": m.get("medianDaysOnMarket"),
                "medianCloseToListRatio": m.get("medianCloseToListRatio"),
                "confidence": (m.get("quality") or {}).get("confidence"),
                "radiusMiles": g.get("radiusMiles"),
                "pointCount": g.get("pointCount"),
            },
        })
    # Only count gaps among areas this export actually asked for; counting
    # every area in market_history would report hundreds of "missing" areas
    # that a --cv export was never going to include.
    have = set(geo_coll.distinct("key", {"scope": scope}))
    for key in sales:
        if key in have:
            continue
        if wanted and (key.split("|")[0] if scope == "subdivision"
                       else key) not in wanted:
            continue
        missing += 1

    feats.sort(key=lambda f: -f["properties"]["sales"])
    out = Path(path).expanduser()
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                              indent=1, default=str))
    print(f"\n🗺️  {len(feats):,} features → {out}")
    if feats:
        top = feats[0]["properties"]
        print(f"    busiest: {top['label']} ({top['city']}) {top['sales']:,} sales")
    if missing:
        print(f"    ⚠️  {missing:,} areas have sales but no geometry "
              "(no usable coordinates) — run without --geojson to build them")


# ──────────────────────────────────────────────────────────────────────────────
# 📋 STATUS
# ──────────────────────────────────────────────────────────────────────────────

def status():
    total = geo_coll.count_documents({})
    if not total:
        print("\n  area_geo is empty — run with --all or --cities first")
        return
    print(f"\narea_geo ({total:,} areas):")
    for r in geo_coll.aggregate([
        {"$group": {"_id": "$scope", "n": {"$sum": 1},
                    "pts": {"$sum": "$pointCount"},
                    "ph": {"$sum": {"$cond": ["$placeholder", 1, 0]}},
                    "wide": {"$sum": {"$cond": [{"$gt": ["$radiusMiles", 3]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ]):
        print(f"  {r['_id']:<12} {r['n']:>6,} areas  {r['pts']:>9,} points"
              f"  {r['ph']:>5,} placeholder  {r['wide']:>4,} wider than 3 mi")

    print("\n  how many market_history areas can be mapped:")
    for scope in SCOPES:
        hk = set(history_coll.distinct("key", {"scope": scope}))
        gk = set(geo_coll.distinct("key", {"scope": scope}))
        if not hk:
            continue
        have = len(hk & gk)
        print(f"  {scope:<12} {have:>6,} of {len(hk):>6,} "
              f"({have/len(hk)*100:>5.1f}%)")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="every city in the feed")
    ap.add_argument("--cities", help="comma-separated city list to limit the scan")
    ap.add_argument("--cv", action="store_true",
                    help="shorthand for the 14 Coachella Valley cities")
    ap.add_argument("--status", action="store_true", help="what is stored, no writes")
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write")
    ap.add_argument("--geojson", metavar="PATH",
                    help="export centroids joined to market_history counts")
    ap.add_argument("--scope", default="subdivision", choices=SCOPES,
                    help="export scope (default subdivision)")
    ap.add_argument("--year", type=int, help="export one year (default: all years summed)")
    ap.add_argument("--type", default="A", choices=("A", "B"), help="export property type")
    ap.add_argument("--min-sales", type=int, default=1, help="export floor")
    ap.add_argument("--include-placeholders", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cities = COACHELLA_VALLEY if args.cv else (
        [c.strip() for c in args.cities.split(",")] if args.cities else None)

    if args.status:
        status()
        return

    if args.geojson:
        export_geojson(args.geojson, args.scope, args.year, args.type, cities,
                       args.min_sales, args.include_placeholders)
        return

    if not (args.all or cities):
        ap.error("pass --all, --cv, --cities, --status or --geojson")

    ensure_indexes()
    started = time.time()
    print(f"\nscanning {'every city' if args.all else f'{len(cities)} cities'}…")
    buckets = collect(cities, args.verbose)
    docs = build_docs(buckets, datetime.now(timezone.utc))
    by_scope = Counter(d["scope"] for d in docs)
    print(f"  {len(docs):,} areas with geometry  "
          + "  ".join(f"{s}={by_scope[s]:,}" for s in SCOPES))
    for d in sorted(docs, key=lambda x: -x["pointCount"])[:5]:
        print(f"    {d['_id']:<44} n={d['pointCount']:>6,}  "
              f"{d['lat']:.4f},{d['lng']:.4f}  r90={d['radiusMiles']:>6.2f} mi"
              + ("  [placeholder]" if d["placeholder"] else ""))
    written = write_docs(docs, args.dry_run)

    print("\n" + "=" * 60)
    print(f"Areas built:   {len(docs):,}")
    print("Dry-run (no writes)" if args.dry_run else f"Written:       {written:,}")
    print(f"Elapsed:       {(time.time()-started)/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
