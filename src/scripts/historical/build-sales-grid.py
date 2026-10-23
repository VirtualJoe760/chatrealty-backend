#!/usr/bin/env python3
"""
Build Sales Grid (sales volume by LOCATION, not by subdivision name)

A heat map built by grouping sales under their `subdivisionName` is wrong, and
wrong in a way that looks fine. 31% of closings valley-wide — and ~89% in CRMLS
territory since Sept 2025 — carry no real subdivision name, so they all collapse
into one bucket per city: "Other" in Palm Desert is 586 sales plotted at a single
centroid, with a 90th-percentile radius of 1.49 miles and a worst point 7.19
miles out. That bucket is not a place. It merges South Palm Desert with north
Palm Desert, and nothing on the map says so.

This script groups by COORDINATE instead. Every closed sale that carries a
latitude and longitude lands in a small fixed grid cell, named or not. South
Palm Desert and Indian Wells fall in different cells because they are in
different places, which is the whole point. The subdivision name stops being the
grouping key and becomes a label looked up per cell.

── Each cell carries its own honesty ────────────────────────────────────────
`namedShare` is the fraction of sales in the cell that carried any subdivision
name. A cell at 0.08 is a real hot spot whose sales simply have no names on
them; it should be labelled by ZIP and city, not silently attributed to whatever
subdivision happens to be closest. The frontend can read that number and decide.

`subdivisionLabel` is the dominant name among the cell's named sales, with
`labelShare` for how dominant. `nearestSubdivision` comes from `area_geo` and is
a pointer, not an assignment — it is the nearest named-subdivision centroid and
its distance, so a cell 0.2 mi from Sun City can say so while a cell 3 mi from
anything says that instead.

── Cell size ────────────────────────────────────────────────────────────────
Default 0.005° ≈ 0.35 mi north-south and 0.29 mi east-west at this latitude.
That is well inside a real subdivision (Sun City's p90 radius is 1.18 mi, Sun
City Shadow Hills 0.93), so a community spans several cells and a hot spot has a
shape rather than a single dot. Larger cells start merging neighbours, which is
the failure this script exists to avoid.

Usage:
    python3 src/scripts/historical/build-sales-grid.py --cv
    python3 src/scripts/historical/build-sales-grid.py --all --cell 0.005
    python3 src/scripts/historical/build-sales-grid.py --status

    # hot spots merged into clusters with a centre and a radius
    python3 src/scripts/historical/build-sales-grid.py --hotspots /tmp/hot.json \\
        --cv --year 2026 --min-sales 10

    # raw cells for a heat layer
    python3 src/scripts/historical/build-sales-grid.py --export /tmp/grid.json --cv
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
DEFAULT_CELL = 0.005          # degrees; ≈0.35 mi lat, ≈0.29 mi lng at 33.7°N
MIN_CELL_SALES = 3            # below this a cell is noise, not a signal
BULK_BATCH = 500
MIN_CLOSE_PRICE = 1000        # matches build-market-history.py
MIN_SQFT = 200
NEAREST_MAX_MI = 5.0          # beyond this, "nearest subdivision" is meaningless

LAT_RANGE = (32.3, 42.1)
LNG_RANGE = (-124.6, -114.0)

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

PROJECTION = {"city": 1, "postalCode": 1, "subdivisionName": 1, "latitude": 1,
              "longitude": 1, "closePrice": 1, "livingArea": 1, "closeDate": 1,
              "propertyType": 1}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
closed_coll = db.unified_closed_listings
geo_coll = db.area_geo
grid_coll = db.sales_grid
print("✅ Connected to MongoDB")


def ensure_indexes():
    grid_coll.create_index([("center", GEOSPHERE)], name="center_2dsphere")
    grid_coll.create_index([("cell", ASCENDING), ("sales", ASCENDING)], name="cell_sales")
    grid_coll.create_index([("city", ASCENDING)], name="city")
    grid_coll.create_index([("postalCode", ASCENDING)], name="postalCode")


# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def is_placeholder(label):
    if not isinstance(label, str):
        return True
    norm = label.strip().lower()
    return norm in PLACEHOLDER_SUBDIVISIONS or bool(PLACEHOLDER_RE.match(norm))


def miles(a, b):
    (la1, ln1), (la2, ln2) = a, b
    p = math.pi / 180
    h = (math.sin((la2 - la1) * p / 2) ** 2
         + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((ln2 - ln1) * p / 2) ** 2)
    return 7917.5 * math.asin(min(1.0, math.sqrt(h)))


def cell_of(lat, lng, cell):
    """Integer grid index. Deterministic, so a re-run upserts the same _id."""
    return (math.floor(lat / cell), math.floor(lng / cell))


def _med(vals, dp=2):
    return round(statistics.median(vals), dp) if vals else None


def load_centroids(cities=None):
    """Named-subdivision centroids from area_geo, for the nearest-area pointer.
    Held in memory: a $near per cell would be thousands of round trips."""
    q = {"scope": "subdivision", "placeholder": False}
    out = []
    for d in geo_coll.find(q, {"key": 1, "label": 1, "city": 1, "lat": 1, "lng": 1}):
        if cities:
            if d["key"].split("|")[0] not in cities:
                continue
        if d.get("lat") is None or d.get("lng") is None:
            continue
        out.append((d["lat"], d["lng"], d.get("label"), d.get("city"), d["key"]))
    return out


def nearest(lat, lng, centroids):
    best, bestd = None, None
    for cla, clng, label, city, key in centroids:
        # Cheap degree-box reject before the trig; at this latitude 0.1° is ~7 mi.
        if abs(cla - lat) > 0.1 or abs(clng - lng) > 0.1:
            continue
        d = miles((lat, lng), (cla, clng))
        if bestd is None or d < bestd:
            best, bestd = (label, city, key), d
    if best is None or bestd > NEAREST_MAX_MI:
        return None, None, None, None
    return best[0], best[1], best[2], round(bestd, 3)


# ──────────────────────────────────────────────────────────────────────────────
# 📍 COLLECT
# ──────────────────────────────────────────────────────────────────────────────

def collect(cell, cities=None, ptype="A", verbose=False):
    q = {"latitude": {"$ne": None}, "longitude": {"$ne": None},
         "propertyType": ptype}
    if cities:
        q["city"] = {"$in": cities}

    cells = defaultdict(lambda: {
        "pts": [], "price": [], "ppsf": [], "years": Counter(),
        "subs": Counter(), "named": 0, "zips": Counter(), "cities": Counter()})
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
        c = cells[cell_of(la, ln, cell)]
        c["pts"].append((la, ln))

        price = d.get("closePrice")
        if isinstance(price, (int, float)) and price >= MIN_CLOSE_PRICE:
            c["price"].append(float(price))
            sqft = d.get("livingArea")
            if isinstance(sqft, (int, float)) and sqft >= MIN_SQFT:
                c["ppsf"].append(float(price) / float(sqft))

        cd = d.get("closeDate")
        if isinstance(cd, datetime):
            c["years"][cd.year] += 1

        sub = (d.get("subdivisionName") or "").strip()
        if sub and not is_placeholder(sub):
            c["subs"][sub] += 1
            c["named"] += 1

        if d.get("postalCode"):
            c["zips"][str(d["postalCode"]).strip()] += 1
        if d.get("city"):
            c["cities"][d["city"].strip()] += 1

    print(f"  scanned {scanned:,} closings, used {kept:,}, "
          f"dropped {dropped:,} for bad or out-of-range coordinates")
    return cells


def build_docs(cells, cell, centroids, now, ptype):
    docs = []
    for (ilat, ilng), c in cells.items():
        n = len(c["pts"])
        if n < MIN_CELL_SALES:
            continue
        clat = ilat * cell + cell / 2
        clng = ilng * cell + cell / 2
        sub, sub_n = (c["subs"].most_common(1)[0] if c["subs"] else (None, 0))
        nl, nc, nk, nd = nearest(clat, clng, centroids)
        docs.append({
            "_id": f"g{cell}:{ilat}:{ilng}:{ptype}",
            "cell": cell,
            "ilat": ilat,
            "ilng": ilng,
            "propertyType": ptype,
            # [lng, lat] — GeoJSON order.
            "center": {"type": "Point", "coordinates": [round(clng, 6), round(clat, 6)]},
            "lat": round(clat, 6),
            "lng": round(clng, 6),
            "bbox": {"minLat": round(ilat * cell, 6), "maxLat": round((ilat + 1) * cell, 6),
                     "minLng": round(ilng * cell, 6), "maxLng": round((ilng + 1) * cell, 6)},
            "sales": n,
            "salesByYear": {str(y): k for y, k in sorted(c["years"].items())},
            "medianClosePrice": _med(c["price"], 0),
            "medianPricePerSqft": _med(c["ppsf"]),
            # The label, and how much to trust it.
            "subdivisionLabel": sub,
            "labelShare": round(sub_n / n, 3) if sub else 0.0,
            "namedShare": round(c["named"] / n, 3),
            "nearestSubdivision": nl,
            "nearestSubdivisionKey": nk,
            "nearestSubdivisionMiles": nd,
            "postalCode": c["zips"].most_common(1)[0][0] if c["zips"] else None,
            "city": c["cities"].most_common(1)[0][0] if c["cities"] else None,
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
        res = grid_coll.bulk_write(
            [UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in chunk],
            ordered=False)
        written += res.upserted_count + res.modified_count
    return written


# ──────────────────────────────────────────────────────────────────────────────
# 🔥 HOTSPOTS
# ──────────────────────────────────────────────────────────────────────────────

def hotspots(cities, year, ptype, min_sales, cell, radius, seed_min):
    """The "top selling areas" view: compact hot zones, each with a centre and a
    radius, labelled from its own contents.

    Peak-based, NOT contiguity-based. The obvious approach — flood-fill every
    touching hot cell — fails badly here: at 0.005° almost every cell in the
    developed valley touches another, so the fill chains across neighbourhoods
    and produces 5-mile blobs that merge Indian Wells into south Palm Desert.
    That is the same error as grouping by subdivision name, just arrived at
    geographically.

    So instead: take the busiest unclaimed cell, absorb everything within
    `radius` miles of it, emit that as one hot spot, repeat. Every cluster is
    bounded by construction, centred on a real peak, and two adjacent busy
    neighbourhoods stay two hot spots. `radius` defaults to 1.25 mi, which fits
    the largest real communities here (Sun City's p90 radius is 1.18 mi) without
    reaching into the next one."""
    q = {"propertyType": ptype, "cell": cell}
    if cities:
        q["city"] = {"$in": cities}
    docs = list(grid_coll.find(q))

    def weight(d):
        return (d["salesByYear"].get(str(year), 0) if year else d["sales"])

    pool = sorted((d for d in docs if weight(d) >= seed_min),
                  key=lambda d: -weight(d))
    used, clusters = set(), []
    for seed in pool:
        if seed["_id"] in used:
            continue
        members = [m for m in pool
                   if m["_id"] not in used
                   and miles((seed["lat"], seed["lng"]), (m["lat"], m["lng"])) <= radius]
        if not members:
            continue
        total = sum(weight(m) for m in members)
        if total < min_sales:
            # Still claim them, or the next pass re-seeds the same cells.
            for m in members:
                used.add(m["_id"])
            continue
        for m in members:
            used.add(m["_id"])

        wlat = sum(m["lat"] * weight(m) for m in members) / total
        wlng = sum(m["lng"] * weight(m) for m in members) / total
        # Half a cell of padding so the drawn circle covers the cells, not just
        # their centre points.
        reach = max(miles((wlat, wlng), (m["lat"], m["lng"])) for m in members)
        names = Counter()
        for m in members:
            if m.get("subdivisionLabel"):
                names[m["subdivisionLabel"]] += weight(m)
        named_sales = sum(round(m["namedShare"] * weight(m)) for m in members)
        prices = [m["medianClosePrice"] for m in members if m.get("medianClosePrice")]
        ppsf = [m["medianPricePerSqft"] for m in members if m.get("medianPricePerSqft")]
        near = Counter()
        for m in members:
            if m.get("nearestSubdivision"):
                near[m["nearestSubdivision"]] += weight(m)
        clusters.append({
            "sales": total,
            "lat": round(wlat, 6), "lng": round(wlng, 6),
            "radiusMiles": round(reach + cell * 40, 2),
            "cells": len(members),
            # How much of this hot spot can be named at all. Low means the
            # sales are real but the MLS carries no subdivision for them, so
            # label it by city and ZIP rather than by the nearest community.
            "namedShare": round(named_sales / total, 3) if total else 0.0,
            "topSubdivisions": [{"label": k, "sales": v} for k, v in names.most_common(3)],
            # What share of the zone the leading name actually accounts for. A
            # zone can be 97% named and still have no single community worth
            # captioning: Palm Springs 92264 runs 221 sales across 31 cells
            # with its top name at 32. Caption by city and ZIP below ~0.5.
            "topShare": round(names.most_common(1)[0][1] / total, 3) if names else 0.0,
            "nearestSubdivision": near.most_common(1)[0][0] if near else None,
            "city": Counter(m["city"] for m in members if m.get("city")).most_common(1)[0][0]
                    if any(m.get("city") for m in members) else None,
            "postalCodes": [z for z, _ in Counter(
                m["postalCode"] for m in members if m.get("postalCode")).most_common(3)],
            "medianClosePrice": _med(prices, 0),
            "medianPricePerSqft": _med(ppsf),
        })
    clusters.sort(key=lambda c: -c["sales"])
    return clusters


# ──────────────────────────────────────────────────────────────────────────────
# 📋 STATUS
# ──────────────────────────────────────────────────────────────────────────────

def status():
    total = grid_coll.count_documents({})
    if not total:
        print("\n  sales_grid is empty — run with --cv or --all first")
        return
    print(f"\nsales_grid ({total:,} cells):")
    for r in grid_coll.aggregate([
        {"$group": {"_id": {"c": "$cell", "t": "$propertyType"},
                    "cells": {"$sum": 1}, "sales": {"$sum": "$sales"},
                    "unnamed": {"$sum": {"$cond": [{"$lt": ["$namedShare", 0.2]}, 1, 0]}}}},
        {"$sort": {"_id.c": 1}},
    ]):
        i = r["_id"]
        print(f"  cell {i['c']}°  type {i['t']}  {r['cells']:>7,} cells  "
              f"{r['sales']:>9,} sales  {r['unnamed']:>6,} cells under 20% named")
    print("\n  busiest cells:")
    for d in grid_coll.find().sort("sales", -1).limit(8):
        lbl = d.get("subdivisionLabel") or f"(unnamed — near {d.get('nearestSubdivision')})"
        print(f"    {d['sales']:>5,} sales  {d['lat']:.4f},{d['lng']:.4f}  "
              f"{str(d.get('city'))[:16]:<16} {str(lbl)[:34]:<34} "
              f"named {d['namedShare']*100:>5.1f}%")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cities")
    ap.add_argument("--cv", action="store_true", help="the 14 Coachella Valley cities")
    ap.add_argument("--cell", type=float, default=DEFAULT_CELL,
                    help=f"grid size in degrees (default {DEFAULT_CELL})")
    ap.add_argument("--type", default="A", choices=("A", "B"))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export", metavar="PATH", help="write cells as JSON for a heat layer")
    ap.add_argument("--hotspots", metavar="PATH", help="write merged hot-spot clusters")
    ap.add_argument("--year", type=int, help="weight by one year (default: all years)")
    ap.add_argument("--min-sales", type=int, default=10, help="hot-spot floor")
    ap.add_argument("--radius", type=float, default=1.25,
                    help="max hot-spot radius in miles (default 1.25)")
    ap.add_argument("--seed-min", type=int, default=MIN_CELL_SALES,
                    help=f"min sales for a cell to join a hot spot (default {MIN_CELL_SALES})")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cities = COACHELLA_VALLEY if args.cv else (
        [c.strip() for c in args.cities.split(",")] if args.cities else None)

    if args.status:
        status()
        return

    if args.hotspots:
        cl = hotspots(cities, args.year, args.type, args.min_sales, args.cell,
                      args.radius, args.seed_min)
        Path(args.hotspots).expanduser().write_text(
            json.dumps({"year": args.year or "all", "propertyType": args.type,
                        "clusters": cl}, indent=1, default=str))
        print(f"\n🔥 {len(cl):,} hot spots → {args.hotspots}")
        for c in cl[:12]:
            top = c["topSubdivisions"][0]["label"] if c["topSubdivisions"] else "—"
            print(f"   {c['sales']:>5,} sales  r={c['radiusMiles']:>5.2f} mi  "
                  f"{str(c['city'])[:16]:<16} named {c['namedShare']*100:>5.1f}%  "
                  f"top: {top}")
        return

    if args.export:
        q = {"propertyType": args.type, "cell": args.cell}
        if cities:
            q["city"] = {"$in": cities}
        rows = []
        for d in grid_coll.find(q):
            w = d["salesByYear"].get(str(args.year), 0) if args.year else d["sales"]
            if not w:
                continue
            rows.append({"lat": d["lat"], "lng": d["lng"], "n": w,
                         "label": d.get("subdivisionLabel"),
                         # labelShare is not decoration. A cell can report
                         # label "Prado" off a single sale in fifteen; without
                         # this number a map captions all fifteen as Prado.
                         "labelShare": d.get("labelShare"),
                         "near": d.get("nearestSubdivision"),
                         "nearMi": d.get("nearestSubdivisionMiles"),
                         "namedShare": d.get("namedShare"),
                         "city": d.get("city"), "zip": d.get("postalCode"),
                         "price": d.get("medianClosePrice"),
                         "ppsf": d.get("medianPricePerSqft")})
        rows.sort(key=lambda r: -r["n"])
        Path(args.export).expanduser().write_text(json.dumps(
            {"cell": args.cell, "year": args.year or "all",
             "propertyType": args.type, "cells": rows}, separators=(",", ":"),
            default=str))
        print(f"\n🗺️  {len(rows):,} cells → {args.export}")
        return

    if not (args.all or cities):
        ap.error("pass --all, --cv, --cities, --status, --export or --hotspots")

    ensure_indexes()
    started = time.time()
    lc = {c.lower() for c in cities} if cities else None
    centroids = load_centroids(lc)
    print(f"\n{len(centroids):,} named-subdivision centroids loaded from area_geo")
    print(f"scanning {'every city' if args.all else f'{len(cities)} cities'} "
          f"at {args.cell}° cells…")
    cells = collect(args.cell, cities, args.type, args.verbose)
    docs = build_docs(cells, args.cell, centroids, datetime.now(timezone.utc), args.type)
    print(f"  {len(docs):,} cells with {MIN_CELL_SALES}+ sales")
    thin = sum(1 for d in docs if d["namedShare"] < 0.2)
    print(f"  {thin:,} of them are under 20% named — these are exactly the cells a "
          "name-grouped map would have mislabelled")
    for d in sorted(docs, key=lambda x: -x["sales"])[:5]:
        lbl = d.get("subdivisionLabel") or f"(unnamed, near {d.get('nearestSubdivision')})"
        print(f"    {d['sales']:>5,} sales  {d['lat']:.4f},{d['lng']:.4f}  "
              f"{str(lbl)[:36]:<36} named {d['namedShare']*100:>5.1f}%")
    written = write_docs(docs, args.dry_run)

    print("\n" + "=" * 60)
    print(f"Cells built:   {len(docs):,}")
    print("Dry-run (no writes)" if args.dry_run else f"Written:       {written:,}")
    print(f"Elapsed:       {(time.time()-started)/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
