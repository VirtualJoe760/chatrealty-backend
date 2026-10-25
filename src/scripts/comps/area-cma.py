#!/usr/bin/env python3
"""
Area CMA — a comparative market analysis for a drawn parcel

The listing CMA needs a subject property. The subdivision CMA needs a name.
A parcel drawn on a map has neither, so this answers the only question a
rectangle can ask: what sold inside it, what is for sale inside it, and how do
those two compare.

Takes coordinates and returns a CMA. The heat map calls it with one grid cell's
`bbox`; the live map calls it with whatever the user drew. Same code path — the
parcel is just a polygon, and a rectangle is a polygon with four corners.

── This file contains no field parsing, and that is the design ──────────────
Every figure it reports is read from `cma_comps` as a stored field.
`pricePerSqft`, `lotSizeSqft`, `daysOnMarket` and `poolKind` are computed once
by `build-cma-comps.py`, which is where the feed's three empty column names and
two unit traps are resolved. If a reader ever has to parse a field here, the
parse belongs in the builder instead.

What is left is one `find` and a median over stored per-item figures. The
`find` is a single query covering both sides, because `cma_comps` holds closed
sales and active listings in one collection with a `status` discriminator —
a parcel query spans both and a two-collection join at read time would be the
thing the invariant exists to prevent.

── Why $geoWithin works here and not on the feed ───────────────────────────
`unified_closed_listings.coordinates` is 2dsphere indexed but present on only
26.5% of Coachella Valley type-A closings, so a geo query against it silently
under-returns — measured on a 2-mile box, 469 rows by geo against 532 by
scalar. The scalar pair is complete but a compound B-tree range-scans only its
leading key, so the same box took 76 seconds.

`cma_comps.center` is built from the scalar pair, so it is complete by
construction and carries a 2dsphere index that bounds the query in both
dimensions. Complete and fast, which neither source index was.

── No scoring, deliberately ────────────────────────────────────────────────
Scoring ranks comps by similarity to a subject, and there is no subject here.
Comps are ordered by `completeness` then recency — the same rule
`build-subdivision-cma.py` uses for a subdivision, for the same reason. The
thresholds and the `good`/`fair`/`insufficient` vocabulary are that script's,
not new ones.
"""

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

DB_NAME = os.getenv("MONGODB_DB", "admin")
db = MongoClient(MONGO_URI)[DB_NAME]

COLL = "cma_comps"

# Borrowed verbatim from build-subdivision-cma.py so a parcel CMA and a
# subdivision CMA do not disagree about what "fair" means.
SAMPLE_WINDOW_MONTHS = 12
INSUFFICIENT_CLOSED_IN_WINDOW = 3
INSUFFICIENT_ACTIVE = 3
CLOSING_CAP = 25
TOP_COMPS = 5
MAJORITY_THRESHOLD = 0.60

# A drawn parcel is meant to be a neighbourhood, not a county. Past this the
# median stops describing one market and the caller should draw smaller.
MAX_ROWS = 25_000

EARTH_RADIUS_MI = 3958.8

COMP_FIELDS = (
    "listingKey", "status", "standardStatus", "price", "pricePerSqft",
    "livingArea", "lotSizeSqft", "lotSizeSource", "beds", "baths",
    "yearBuilt", "poolKind", "hasPrivatePool", "spaKind", "daysOnMarket",
    "eventDate", "closePrice", "listPrice", "originalListPrice",
    "saleToListRatio", "lat", "lng", "city", "postalCode",
    "subdivisionName", "address", "slug", "completeness", "propertySubType",
)


# ── parcel geometry ──────────────────────────────────────────────────────────

def parse_bbox(s):
    """minLng,minLat,maxLng,maxLat — the order the heat map already emits."""
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError("--bbox needs minLng,minLat,maxLng,maxLat")
    min_lng, min_lat, max_lng, max_lat = parts
    if min_lng >= max_lng or min_lat >= max_lat:
        raise ValueError("--bbox min must be less than max on both axes")
    return min_lng, min_lat, max_lng, max_lat


def bbox_to_ring(b):
    min_lng, min_lat, max_lng, max_lat = b
    return [[min_lng, min_lat], [max_lng, min_lat], [max_lng, max_lat],
            [min_lng, max_lat], [min_lng, min_lat]]


def parse_polygon(spec):
    """One string of `lng,lat` pairs separated by spaces or semicolons.

    A single argument rather than nargs="+" because every longitude in this
    market is negative, and argparse reads a leading `-` as an option flag.
    """
    ring = []
    for tok in spec.replace(";", " ").split():
        lng, lat = (float(x) for x in tok.split(","))
        ring.append([lng, lat])
    if len(ring) < 3:
        raise ValueError("--polygon needs at least 3 points")
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def ring_bbox(ring):
    lngs = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lngs), min(lats), max(lngs), max(lats)


def ring_centroid(ring):
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    return [round(sum(p[0] for p in pts) / len(pts), 6),
            round(sum(p[1] for p in pts) / len(pts), 6)]


def ring_area_sq_mi(ring):
    """Shoelace on an equirectangular projection about the ring's own latitude.
    Describes the parcel the caller passed in; it reads no listing data."""
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    lat0 = math.radians(sum(p[1] for p in pts) / len(pts))
    deg_lat_mi = math.pi * EARTH_RADIUS_MI / 180.0
    deg_lng_mi = deg_lat_mi * math.cos(lat0)
    acc = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i][0] * deg_lng_mi, pts[i][1] * deg_lat_mi
        x2, y2 = pts[(i + 1) % len(pts)][0] * deg_lng_mi, pts[(i + 1) % len(pts)][1] * deg_lat_mi
        acc += x1 * y2 - x2 * y1
    return round(abs(acc) / 2.0, 4)


# ── the one query ────────────────────────────────────────────────────────────

def fetch(ring, ptype, months, sub_types=None):
    """One find. Both sides, stored fields only, no aggregation stage."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=round(months * 30.44))
    q = {
        "propertyType": ptype,
        "center": {"$geoWithin": {"$geometry": {"type": "Polygon", "coordinates": [ring]}}},
        "$or": [
            {"status": "active"},
            {"status": "closed", "eventDate": {"$gte": since}},
        ],
    }
    if sub_types:
        q["propertySubType"] = {"$in": sub_types}

    n = db[COLL].count_documents(q)
    if n > MAX_ROWS:
        raise SystemExit(
            f"parcel holds {n:,} rows (cap {MAX_ROWS:,}). A median over that many "
            "is describing several markets at once — draw a smaller parcel.")

    proj = {f: 1 for f in COMP_FIELDS}
    proj["_id"] = 0
    rows = list(db[COLL].find(q, proj))
    return rows, since


# ── rollups: median over stored per-item figures ─────────────────────────────

def block(rows, field):
    vals = sorted(r[field] for r in rows if r.get(field) is not None)
    if not vals:
        return {"count": 0, "median": None, "avg": None, "min": None,
                "max": None, "p25": None, "p75": None}

    def pct(p):
        if len(vals) == 1:
            return vals[0]
        i = (len(vals) - 1) * p
        lo, hi = math.floor(i), math.ceil(i)
        return vals[lo] if lo == hi else vals[lo] + (vals[hi] - vals[lo]) * (i - lo)

    return {
        "count": len(vals),
        "median": round(statistics.median(vals), 2),
        "avg": round(sum(vals) / len(vals), 2),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
        "p25": round(pct(0.25), 2),
        "p75": round(pct(0.75), 2),
    }


def amenity_mix(rows, field):
    mix = Counter(r.get(field) or "unknown" for r in rows)
    known = sum(v for k, v in mix.items() if k != "unknown")
    return {
        "mix": dict(mix.most_common()),
        "knownCount": known,
        "privateShare": round(mix.get("private", 0) / known, 4) if known else None,
    }


def side_summary(rows, with_sale_to_list):
    out = {
        "count": len(rows),
        "price": block(rows, "price"),
        "pricePerSqft": block(rows, "pricePerSqft"),
        "livingArea": block(rows, "livingArea"),
        "lotSizeSqft": block(rows, "lotSizeSqft"),
        "daysOnMarket": block(rows, "daysOnMarket"),
        "yearBuilt": block(rows, "yearBuilt"),
        "beds": block(rows, "beds"),
        "baths": block(rows, "baths"),
        "pool": amenity_mix(rows, "poolKind"),
        "spa": amenity_mix(rows, "spaKind"),
    }
    if with_sale_to_list:
        out["saleToListRatio"] = block(rows, "saleToListRatio")
    return out


def top_comps(rows, n):
    """completeness then recency — no scoring, because there is no subject."""
    ordered = sorted(
        rows,
        key=lambda r: (r.get("completeness") or 0,
                       r.get("eventDate") or datetime.min),
        reverse=True)
    return ordered[:n]


def quality(closed, active):
    notes = []
    if len(closed) < INSUFFICIENT_CLOSED_IN_WINDOW:
        notes.append(f"Fewer than {INSUFFICIENT_CLOSED_IN_WINDOW} closings in "
                     f"the window.")
    if len(active) < INSUFFICIENT_ACTIVE:
        notes.append(f"Fewer than {INSUFFICIENT_ACTIVE} active listings.")

    unknown_pool = sum(1 for r in closed if (r.get("poolKind") or "unknown") == "unknown")
    if closed and unknown_pool / len(closed) > 0.25:
        notes.append(f"poolFeatures absent on {unknown_pool/len(closed)*100:.0f}% "
                     "of closings; pool mix is partial.")

    inferred = sum(1 for r in closed if str(r.get("lotSizeSource", "")).startswith("area_inferred"))
    if closed and inferred / len(closed) > 0.5:
        notes.append(f"lotSizeSqft inferred from undeclared units on "
                     f"{inferred/len(closed)*100:.0f}% of closings.")

    if len(closed) < INSUFFICIENT_CLOSED_IN_WINDOW and len(active) < INSUFFICIENT_ACTIVE:
        conf = "insufficient"
    elif len(closed) >= CLOSING_CAP and len(active) >= INSUFFICIENT_ACTIVE * 2:
        conf = "good"
    else:
        conf = "fair"
    return {"confidence": conf, "notes": notes}


def build(ring, ptype, months, sub_types, n_comps):
    rows, since = fetch(ring, ptype, months, sub_types)
    closed = [r for r in rows if r["status"] == "closed"]
    active = [r for r in rows if r["status"] == "active"]

    absorption = None
    months_inventory = None
    if active:
        absorption = round((len(closed) / months) / len(active), 4)
        if closed:
            months_inventory = round(len(active) / (len(closed) / months), 2)

    cities = Counter(r.get("city") for r in closed + active if r.get("city"))
    subs = Counter(r.get("subdivisionName") for r in closed + active
                   if r.get("subdivisionName"))

    return {
        "parcel": {
            "ring": ring,
            "bbox": list(ring_bbox(ring)),
            "centroid": ring_centroid(ring),
            "areaSqMi": ring_area_sq_mi(ring),
            "corners": len(ring) - 1,
        },
        "window": {
            "months": months,
            "closedSince": since.isoformat(),
            "propertyType": ptype,
            "propertySubTypes": sub_types or "all",
        },
        "closed": side_summary(closed, with_sale_to_list=True),
        "active": side_summary(active, with_sale_to_list=False),
        "absorptionRate": absorption,
        "monthsOfInventory": months_inventory,
        "place": {
            "cities": dict(cities.most_common(5)),
            "subdivisions": dict(subs.most_common(5)),
            "dominantCity": cities.most_common(1)[0][0] if cities else None,
        },
        "comps": {
            "closed": top_comps(closed, n_comps),
            "active": top_comps(active, n_comps),
        },
        "quality": quality(closed, active),
        "source": {"collection": COLL, "readSurface": "one find + medians over stored fields"},
    }


# ── output ───────────────────────────────────────────────────────────────────

def money(v):
    return "—" if v is None else f"${v:,.0f}"


def render(r):
    p, w = r["parcel"], r["window"]
    print("=" * 68)
    print(f"Area CMA — {p['areaSqMi']} sq mi, {p['corners']} corners, "
          f"centroid {p['centroid'][1]:.5f}, {p['centroid'][0]:.5f}")
    print(f"Type {w['propertyType']}   closed window {w['months']} months   "
          f"{r['place']['dominantCity'] or 'unknown city'}")
    print("=" * 68)

    for side in ("closed", "active"):
        s = r[side]
        label = "CLOSED SALES" if side == "closed" else "ACTIVE LISTINGS"
        print(f"\n{label}  n={s['count']:,}")
        if not s["count"]:
            continue
        pr, pp = s["price"], s["pricePerSqft"]
        print(f"  price        median {money(pr['median']):>12s}   "
              f"range {money(pr['min'])} – {money(pr['max'])}")
        print(f"  $/sqft       median {pp['median'] if pp['median'] else '—':>12}   "
              f"p25–p75 {pp['p25']} – {pp['p75']}")
        print(f"  living area  median {s['livingArea']['median']}   "
              f"lot sqft median {s['lotSizeSqft']['median']}")
        print(f"  dom          median {s['daysOnMarket']['median']}   "
              f"year built median {s['yearBuilt']['median']}")
        print(f"  beds/baths   {s['beds']['median']} / {s['baths']['median']}")
        pool = s["pool"]
        share = pool["privateShare"]
        print(f"  pool         private {f'{share*100:.0f}%' if share is not None else '—'}"
              f"  mix {pool['mix']}")
        if side == "closed" and s.get("saleToListRatio", {}).get("median"):
            print(f"  sale/list    median {s['saleToListRatio']['median']}")

    print(f"\nabsorption rate      {r['absorptionRate']}")
    print(f"months of inventory  {r['monthsOfInventory']}")
    print(f"subdivisions seen    {r['place']['subdivisions']}")
    q = r["quality"]
    print(f"\nconfidence: {q['confidence']}")
    for n in q["notes"]:
        print(f"  • {n}")

    for side in ("closed", "active"):
        comps = r["comps"][side]
        if not comps:
            continue
        print(f"\ntop {side} comps (completeness, then recency)")
        for c in comps:
            print(f"  {str(c.get('eventDate'))[:10]}  {money(c.get('price')):>12s}  "
                  f"{str(c.get('livingArea') or '—'):>5} sqft  "
                  f"{c.get('beds') or '—'}bd/{c.get('baths') or '—'}ba  "
                  f"pool={c.get('poolKind'):9s}  c={c.get('completeness')}  "
                  f"{(c.get('address') or '')[:34]}")
    print()


def normalize_argv(argv):
    """Rewrite `--bbox -116.3,33.7` into `--bbox=-116.3,33.7`.

    Longitudes here are always negative, so the value of a coordinate option
    starts with `-` and argparse would read it as an unknown option. Only the
    coordinate options are touched, and only when the value looks like a
    coordinate rather than a flag.
    """
    coord_opts = {"--bbox", "--polygon"}
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok in coord_opts and i + 1 < len(argv):
            val = argv[i + 1]
            if val.startswith("-") and any(c.isdigit() for c in val[:3]):
                out.append(f"{tok}={val}")
                i += 2
                continue
        out.append(tok)
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser(
        description="CMA for a drawn parcel — one find over cma_comps")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="minLng,minLat,maxLng,maxLat")
    g.add_argument("--polygon", metavar='"LNG,LAT LNG,LAT ..."',
                   help="arbitrary ring, one quoted string; closed automatically")
    g.add_argument("--cell", help="a sales_grid _id — uses that cell's bbox")
    ap.add_argument("--type", default="A", help="propertyType (A sale, B lease)")
    ap.add_argument("--months", type=int, default=SAMPLE_WINDOW_MONTHS,
                    help=f"closed window (default {SAMPLE_WINDOW_MONTHS})")
    ap.add_argument("--sub-type", nargs="+", dest="sub_types",
                    help="limit to propertySubType values")
    ap.add_argument("--comps", type=int, default=TOP_COMPS,
                    help=f"comps listed per side (default {TOP_COMPS})")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args(normalize_argv(sys.argv[1:]))

    if args.cell:
        cell = db["sales_grid"].find_one({"_id": args.cell}, {"bbox": 1})
        if not cell or not cell.get("bbox"):
            raise SystemExit(f"no sales_grid cell {args.cell!r} with a bbox")
        b = cell["bbox"]
        ring = bbox_to_ring((b["minLng"], b["minLat"], b["maxLng"], b["maxLat"]))
    elif args.bbox:
        ring = bbox_to_ring(parse_bbox(args.bbox))
    else:
        ring = parse_polygon(args.polygon)

    result = build(ring, args.type, args.months, args.sub_types, args.comps)

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        render(result)


if __name__ == "__main__":
    main()
