#!/usr/bin/env python3
"""
Build Sales Hotspots (the zone layer, precomputed because clustering is a calculation)

`sales_grid` gives the heat layer and `area_geo` the area circles. The zone layer
— "the top selling areas", each with a centre, a radius and a caption — needs
clustering, and clustering at request time would break the invariant that the
read surface never calculates. It would also be non-deterministic across
requests, so zones would shift and relabel as the user pans. So it is computed
once here and stored.

One document per zone, per year, per property type, plus an `allYears` companion.

── Zone geometry is clustered ONCE, then reported per year ──────────────────
Membership comes from all-time sales and is identical in every year's document;
only the figures change. That is deliberate. Clustering each year separately
would make zones appear, split and drift as a user drags a year slider, and
every one of those movements would be an artefact of the clustering rather than
a fact about the market. An area that is busy in only one year still forms a
zone, because its sales are in the all-time total too.

Each year's document still carries its OWN centre, radius, medians and label,
weighted by that year's sales, so the figures never borrow from another year.

── Medians come from the sales, never from the cells ────────────────────────
A zone's `medianClosePrice` is the median over every closed sale inside its
member cells. It is NOT the mean or median of those cells' `medianClosePrice`
values: a median of medians is not a median, and across cells of unequal size it
is not close. The same applies to `namedShare` and `labelShare`, which
`sales_grid` only stores pooled across all years.

So this script scans the closings once and computes all of it per zone-year from
source. That costs a full pass and buys figures that are exactly what they claim
to be.

── Why not DBSCAN ───────────────────────────────────────────────────────────
DBSCAN at eps 0.75 mi was the suggested starting point, and density-connectivity
is the one thing that cannot work here. It chains: at 0.005° nearly every cell
in the developed valley is within 0.75 mi of another, so a connected component
runs across neighbourhood boundaries. This was measured on exactly this data —
a flood fill over touching cells produced 5-mile zones, one merging 853 Palm
Springs sales and another 722 in Rancho Mirage, putting Indian Wells and south
Palm Desert in a single bucket.

So zones are radius-bounded peaks instead: take the busiest unclaimed cell,
absorb every unclaimed cell within `--radius`, emit, repeat. Every zone is
bounded by construction and centred on a real peak, so two adjacent busy
neighbourhoods stay two zones. Parameters land in the `hs:meta` document.

Determinism: cells are seeded in (sales desc, ilat, ilng) order and zones are
ranked by sales with the seed cell's (ilat, ilng) as tiebreak, so the same input
yields the same membership, the same ranks and the same `_id`s.

Usage:
    python3 src/scripts/historical/build-sales-hotspots.py --cv
    python3 src/scripts/historical/build-sales-hotspots.py --all
    python3 src/scripts/historical/build-sales-hotspots.py --status
    python3 src/scripts/historical/build-sales-hotspots.py --all --radius 1.25 --dry-run
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
DEFAULT_CELL = 0.005
DEFAULT_RADIUS = 1.0        # mi; see --radius note in the meta doc
MIN_ZONE_SALES = 25         # all-time total required to form a zone
MIN_YEAR_SALES = 10         # a zone-year below this is not published
MIN_CELLS = 2               # drop singleton cells
MIN_CLOSE_PRICE = 1000
MIN_SQFT = 200
BULK_BATCH = 500

NAMED_FLOOR = 0.20          # below: caption by city + ZIP
LABEL_FLOOR = 0.50          # below: caption by city, "multiple communities"

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
              "countyOrParish": 1}

META_ID = "hs:meta"

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
closed_coll = db.unified_closed_listings
grid_coll = db.sales_grid
history_coll = db.market_history
hot_coll = db.sales_hotspots
print("✅ Connected to MongoDB")


def ensure_indexes():
    hot_coll.create_index([("propertyType", ASCENDING), ("year", ASCENDING),
                           ("rank", ASCENDING)], name="type_year_rank")
    hot_coll.create_index([("center", GEOSPHERE)], name="center_2dsphere")
    hot_coll.create_index([("city", ASCENDING), ("year", ASCENDING)], name="city_year")


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


def _med(vals, dp=2):
    return round(statistics.median(vals), dp) if vals else None


def _pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def year_coverage():
    """Mirror market_history rather than recompute: it already stores how much of
    each calendar year the collection holds, and two answers to one question is
    how they drift apart."""
    out = {}
    for d in history_coll.find({"scope": "city"},
                               {"year": 1, "partialYear": 1, "yearMonthsCovered": 1}):
        y = d["year"]
        if y not in out:
            out[y] = (bool(d.get("partialYear")), d.get("yearMonthsCovered"))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 🧩 CLUSTER
# ──────────────────────────────────────────────────────────────────────────────

def cluster(cells, radius, min_zone, min_cells):
    """Radius-bounded peaks over all-time sales. Deterministic: seeds are taken
    in (sales desc, ilat, ilng) order, so ties cannot reshuffle between builds.

    A seed is only ever compared against cells in its own coarse bucket and the
    eight around it. Scanning the whole pool per seed is the obvious way to write
    this and it does not survive the statewide run: 34,082 cells against a few
    thousand seeds is on the order of 10^8 haversine calls. The bucket edge is
    sized from `radius`, so the 3x3 neighbourhood always covers the search disc."""
    pool = sorted(cells, key=lambda c: (-c["sales"], c["ilat"], c["ilng"]))
    # 69 mi per degree of latitude; 1.5x margin so the 3x3 window is sufficient
    # even where a degree of longitude is shorter.
    coarse = max(0.02, radius / 69.0 * 1.5)
    buckets = defaultdict(list)
    for c in pool:
        buckets[(math.floor(c["lat"] / coarse), math.floor(c["lng"] / coarse))].append(c)

    claimed, zones = set(), []
    for seed in pool:
        if seed["_id"] in claimed:
            continue
        bi = math.floor(seed["lat"] / coarse)
        bj = math.floor(seed["lng"] / coarse)
        members = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for c in buckets.get((bi + di, bj + dj), ()):
                    if c["_id"] in claimed:
                        continue
                    if miles((seed["lat"], seed["lng"]), (c["lat"], c["lng"])) <= radius:
                        members.append(c)
        if not members:
            continue
        for m in members:
            claimed.add(m["_id"])
        total = sum(m["sales"] for m in members)
        if total < min_zone or len(members) < min_cells:
            continue
        # Membership order must not depend on bucket iteration order.
        members.sort(key=lambda c: (c["ilat"], c["ilng"]))
        zones.append({"seed": (seed["ilat"], seed["ilng"]), "members": members})
    # Stable order independent of discovery order.
    zones.sort(key=lambda z: (-sum(m["sales"] for m in z["members"]), z["seed"]))
    return zones


# ──────────────────────────────────────────────────────────────────────────────
# 📍 SCAN SOURCE
# ──────────────────────────────────────────────────────────────────────────────

def scan_source(zones, cell, ptype, cities, verbose=False):
    """One pass over the closings, accumulating per zone-year from source.

    Everything that must not be a median-of-medians is built here: prices, ppsf,
    the named-name counts behind namedShare/labelShare, and the dominant city,
    ZIP and county."""
    cell_zone = {}
    for zi, z in enumerate(zones):
        for m in z["members"]:
            cell_zone[(m["ilat"], m["ilng"])] = zi

    def blank():
        return {"price": [], "ppsf": [], "n": 0, "named": 0, "subs": Counter(),
                "zips": Counter(), "cities": Counter(), "counties": Counter()}

    # acc[zone_index][year or "all"]
    acc = defaultdict(lambda: defaultdict(blank))

    q = {"latitude": {"$ne": None}, "longitude": {"$ne": None},
         "propertyType": ptype}
    if cities:
        q["city"] = {"$in": cities}

    scanned = hit = 0
    for d in closed_coll.find(q, PROJECTION):
        scanned += 1
        if verbose and scanned % 200_000 == 0:
            print(f"    … {scanned:,} scanned")
        la, ln = d.get("latitude"), d.get("longitude")
        if not isinstance(la, (int, float)) or not isinstance(ln, (int, float)):
            continue
        if not (LAT_RANGE[0] <= la <= LAT_RANGE[1]
                and LNG_RANGE[0] <= ln <= LNG_RANGE[1]):
            continue
        key = (math.floor(la / cell), math.floor(ln / cell))
        zi = cell_zone.get(key)
        if zi is None:
            continue
        hit += 1

        cd = d.get("closeDate")
        year = cd.year if isinstance(cd, datetime) else None
        buckets = [acc[zi]["all"]]
        if year is not None:
            buckets.append(acc[zi][year])

        price = d.get("closePrice")
        ok_price = isinstance(price, (int, float)) and price >= MIN_CLOSE_PRICE
        sqft = d.get("livingArea")
        ok_sqft = ok_price and isinstance(sqft, (int, float)) and sqft >= MIN_SQFT
        sub = (d.get("subdivisionName") or "").strip()
        real = bool(sub) and not is_placeholder(sub)

        for b in buckets:
            b["n"] += 1
            if ok_price:
                b["price"].append(float(price))
                if ok_sqft:
                    b["ppsf"].append(float(price) / float(sqft))
            if real:
                b["named"] += 1
                b["subs"][sub] += 1
            if d.get("postalCode"):
                b["zips"][str(d["postalCode"]).strip()] += 1
            if d.get("city"):
                b["cities"][d["city"].strip()] += 1
            if d.get("countyOrParish"):
                b["counties"][str(d["countyOrParish"]).strip()] += 1

    print(f"  scanned {scanned:,} closings, {hit:,} fell inside a zone")
    return acc


# ──────────────────────────────────────────────────────────────────────────────
# 🏷️ LABEL
# ──────────────────────────────────────────────────────────────────────────────

def decide_label(bucket, city, zipcode):
    """The caption, decided here and stored. `labelKind` is what the UI renders
    on, so it never re-derives the rule and the two can never disagree.

    Both shares come from the sales themselves, not from cell averages, and they
    catch different failures: namedShare asks whether this place has names at
    all, labelShare whether any one community actually dominates. A zone can be
    99% named and still have no nameable community."""
    n = bucket["n"]
    named_share = round(bucket["named"] / n, 3) if n else 0.0
    top, top_n = (bucket["subs"].most_common(1)[0] if bucket["subs"] else (None, 0))
    label_share = round(top_n / n, 3) if n else 0.0

    if named_share < NAMED_FLOOR:
        return {"label": f"{city} {zipcode}".strip() if city else (zipcode or "Unknown"),
                "labelKind": "city_zip", "namedShare": named_share,
                "labelShare": label_share, "dominantSubdivision": None}
    if label_share < LABEL_FLOOR:
        return {"label": f"{city} — multiple communities" if city else "Multiple communities",
                "labelKind": "city_multiple", "namedShare": named_share,
                "labelShare": label_share, "dominantSubdivision": None}
    return {"label": top, "labelKind": "subdivision", "namedShare": named_share,
            "labelShare": label_share, "dominantSubdivision": top}


# ──────────────────────────────────────────────────────────────────────────────
# 🧱 BUILD
# ──────────────────────────────────────────────────────────────────────────────

def build_docs(zones, acc, cell, ptype, radius, now, coverage,
               min_year, years_wanted):
    """One doc per (zone, year) plus the allYears companion. Each carries its own
    centre, radius, medians and label for its own period."""
    rows = []
    for zi, z in enumerate(zones):
        members = z["members"]
        periods = [("all", True)] + [(y, False) for y in years_wanted]
        for period, is_all in periods:
            bucket = acc[zi].get(period)
            if not bucket or bucket["n"] == 0:
                continue
            # Weight by the sales of THIS period, so a year's geometry is its own.
            if is_all:
                w = {(m["ilat"], m["ilng"]): m["sales"] for m in members}
            else:
                w = {(m["ilat"], m["ilng"]): m["salesByYear"].get(str(period), 0)
                     for m in members}
            total = sum(w.values())
            if total < (MIN_ZONE_SALES if is_all else min_year):
                continue
            live = [m for m in members if w[(m["ilat"], m["ilng"])] > 0]
            if not live:
                continue
            wlat = sum(m["lat"] * w[(m["ilat"], m["ilng"])] for m in live) / total
            wlng = sum(m["lng"] * w[(m["ilat"], m["ilng"])] for m in live) / total
            dists = sorted(miles((wlat, wlng), (m["lat"], m["lng"])) for m in live)

            city = bucket["cities"].most_common(1)[0][0] if bucket["cities"] else None
            zipc = bucket["zips"].most_common(1)[0][0] if bucket["zips"] else None
            county = bucket["counties"].most_common(1)[0][0] if bucket["counties"] else None
            lab = decide_label(bucket, city, zipc)
            partial, months = coverage.get(period, (None, None)) if not is_all else (None, None)

            rows.append({
                "propertyType": ptype,
                "year": None if is_all else period,
                "allYears": is_all,
                # Stable across builds and across years: derived from the seed
                # cell, not from rank. The UI tracks a zone with this; `_id`
                # carries rank because the tab's primary query sorts on it.
                "zoneId": f"z{cell}:{z['seed'][0]}:{z['seed'][1]}:{ptype}",
                "seedCell": list(z["seed"]),
                "center": {"type": "Point", "coordinates": [round(wlng, 6), round(wlat, 6)]},
                "lat": round(wlat, 6), "lng": round(wlng, 6),
                "radiusMiles": round(_pct(dists, 0.90) + cell * 40, 3),
                "maxRadiusMiles": round(dists[-1] + cell * 40, 3),
                "bbox": {
                    "minLat": round(min(m["bbox"]["minLat"] for m in live), 6),
                    "maxLat": round(max(m["bbox"]["maxLat"] for m in live), 6),
                    "minLng": round(min(m["bbox"]["minLng"] for m in live), 6),
                    "maxLng": round(max(m["bbox"]["maxLng"] for m in live), 6),
                },
                "sales": total,
                "cellCount": len(live),
                **lab,
                "city": city,
                "postalCode": zipc,
                "county": county,
                # Medians over the underlying sales in this period, never over
                # the member cells' medians.
                "medianClosePrice": _med(bucket["price"], 0),
                "medianPricePerSqft": _med(bucket["ppsf"]),
                "salesWithPrice": len(bucket["price"]),
                "partialYear": partial,
                "yearMonthsCovered": months,
                "memberCellIds": sorted(m["_id"] for m in live),
                "cell": cell,
                "radiusParam": radius,
                "source": "sales_grid",
                "builtAt": now,
            })

    # Rank within each (propertyType, period). Tiebreak on the seed cell so a
    # tie cannot reshuffle the map between builds.
    docs = []
    by_period = defaultdict(list)
    for r in rows:
        by_period[(r["propertyType"], r["allYears"], r["year"])].append(r)
    for key, group in by_period.items():
        group.sort(key=lambda r: (-r["sales"], r["seedCell"][0], r["seedCell"][1]))
        for i, r in enumerate(group, 1):
            r["rank"] = i
            tag = "all" if r["allYears"] else str(r["year"])
            r["_id"] = f"hs:{r['propertyType']}:{tag}:{i:05d}"
            docs.append(r)
    return docs


def write_docs(docs, ptype, cities, dry_run):
    """Upsert, then drop any doc THIS RUN'S SCOPE should have produced and did
    not. `_id` carries rank, so a period that shrinks would otherwise leave
    high-ranked orphans behind and the tab would serve them.

    The delete is scoped to the cities being rebuilt. Deleting every doc for the
    property type instead would mean a `--cv` run silently wipes the 2,000+
    zones outside the valley that an earlier `--all` wrote — and there are no
    database backups to recover them from."""
    if dry_run:
        return 0, 0
    written = 0
    for i in range(0, len(docs), BULK_BATCH):
        chunk = docs[i:i + BULK_BATCH]
        res = hot_coll.bulk_write(
            [UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in chunk],
            ordered=False)
        written += res.upserted_count + res.modified_count
    keep = [d["_id"] for d in docs]
    # META_ID is excluded explicitly. It is not a zone, and letting the sweep
    # delete and the upsert recreate it every run reports a phantom "stale
    # removed: 1" and leaves the build parameters momentarily absent.
    scope = {"propertyType": ptype, "_id": {"$nin": keep + [META_ID]}}
    if cities:
        scope["city"] = {"$in": cities}
    stale = hot_coll.delete_many(scope)
    return written, stale.deleted_count


# ──────────────────────────────────────────────────────────────────────────────
# 📋 STATUS
# ──────────────────────────────────────────────────────────────────────────────

def status():
    n = hot_coll.count_documents({"_id": {"$ne": META_ID}})
    if not n:
        print("\n  sales_hotspots is empty — run with --cv or --all first")
        return
    meta = hot_coll.find_one({"_id": META_ID})
    if meta:
        p = meta.get("params", {})
        print(f"\nbuild params: radius={p.get('radiusMiles')} mi  cell={p.get('cell')}°  "
              f"minZone={p.get('minZoneSales')}  minYear={p.get('minYearSales')}  "
              f"minCells={p.get('minCells')}")
        print(f"             algorithm: {p.get('algorithm')}")
    print(f"\nsales_hotspots ({n:,} zone-period docs):")
    for r in hot_coll.aggregate([
        {"$match": {"_id": {"$ne": META_ID}}},
        {"$group": {"_id": {"t": "$propertyType", "y": "$year", "a": "$allYears"},
                    "zones": {"$sum": 1}, "sales": {"$sum": "$sales"}}},
        {"$sort": {"_id.t": 1, "_id.a": -1, "_id.y": 1}},
    ]):
        i = r["_id"]
        tag = "all years" if i["a"] else str(i["y"])
        print(f"  type {i['t']}  {tag:<10} {r['zones']:>5,} zones  {r['sales']:>8,} sales")
    print("\n  caption mix:")
    for r in hot_coll.aggregate([
        {"$match": {"_id": {"$ne": META_ID}}},
        {"$group": {"_id": "$labelKind", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        print(f"    {str(r['_id']):<16} {r['n']:>5,}")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cities")
    ap.add_argument("--cv", action="store_true", help="the 14 Coachella Valley cities")
    ap.add_argument("--cell", type=float, default=DEFAULT_CELL)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS,
                    help=f"max zone radius in miles (default {DEFAULT_RADIUS})")
    ap.add_argument("--type", default="A", choices=("A", "B"))
    ap.add_argument("--min-zone-sales", type=int, default=MIN_ZONE_SALES)
    ap.add_argument("--min-year-sales", type=int, default=MIN_YEAR_SALES)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return

    cities = COACHELLA_VALLEY if args.cv else (
        [c.strip() for c in args.cities.split(",")] if args.cities else None)
    if not (args.all or cities):
        ap.error("pass --all, --cv, --cities or --status")

    started = time.time()
    if not args.dry_run:
        ensure_indexes()
    gq = {"propertyType": args.type, "cell": args.cell}
    if cities:
        gq["city"] = {"$in": cities}
    cells = list(grid_coll.find(gq))
    if not cells:
        print(f"\n⚠️  no sales_grid cells for type {args.type} at {args.cell}° — "
              "run build-sales-grid.py first")
        return
    print(f"\n{len(cells):,} grid cells loaded")

    zones = cluster(cells, args.radius, args.min_zone_sales, MIN_CELLS)
    print(f"{len(zones):,} zones formed at radius {args.radius} mi "
          f"(min {args.min_zone_sales} all-time sales, min {MIN_CELLS} cells)")

    coverage = year_coverage()
    years = sorted(y for y in coverage)
    print(f"years from market_history: {', '.join(str(y) for y in years)}")

    acc = scan_source(zones, args.cell, args.type, cities, args.verbose)
    docs = build_docs(zones, acc, args.cell, args.type, args.radius,
                      datetime.now(timezone.utc), coverage,
                      args.min_year_sales, years)
    per = Counter("all years" if d["allYears"] else d["year"] for d in docs)
    print(f"\n{len(docs):,} zone-period docs:")
    for k in sorted(per, key=str):
        print(f"  {str(k):<10} {per[k]:>5,} zones")
    kinds = Counter(d["labelKind"] for d in docs)
    print(f"  captions: " + "  ".join(f"{k}={v:,}" for k, v in kinds.most_common()))

    written, stale = write_docs(docs, args.type, cities, args.dry_run)
    if not args.dry_run:
        hot_coll.update_one({"_id": META_ID}, {"$set": {
            "_id": META_ID,
            "params": {
                "algorithm": "radius-bounded peak clustering over sales_grid cells; "
                             "seeds in (sales desc, ilat, ilng) order; a seed absorbs "
                             "every unclaimed cell within radiusMiles; NOT DBSCAN — "
                             "density-connectivity chains across neighbourhoods at "
                             "this cell size",
                "radiusMiles": args.radius, "cell": args.cell,
                "minZoneSales": args.min_zone_sales,
                "minYearSales": args.min_year_sales, "minCells": MIN_CELLS,
                "namedFloor": NAMED_FLOOR, "labelFloor": LABEL_FLOOR,
                "membership": "clustered once on all-time sales; identical in every "
                              "year's doc, so zones do not drift on a year slider",
                "medians": "computed from unified_closed_listings, not from cell medians",
                "propertyType": args.type,
                "scope": cities or "all",
            },
            # propertyType and scope live INSIDE params on purpose. At the top
            # level, `propertyType` makes this doc match find({propertyType:"A"})
            # — any read route that filters without `year` would serve the
            # parameters block as if it were a zone.
            "builtAt": datetime.now(timezone.utc),
        }}, upsert=True)

    print("\n" + "=" * 60)
    print(f"Zones formed:  {len(zones):,}")
    print(f"Docs built:    {len(docs):,}")
    print("Dry-run (no writes)" if args.dry_run
          else f"Written:       {written:,}\nStale removed: {stale:,}")
    print(f"Elapsed:       {(time.time()-started)/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
