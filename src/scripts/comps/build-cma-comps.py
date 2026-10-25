#!/usr/bin/env python3
"""
Build CMA Comps (one comparable per document, normalised and placed)

The listing-level CMA scores comps against a subject property. The subdivision
CMA describes a named community. Neither can answer "what happened inside this
rectangle", because a rectangle has no subject and no name — and that is the
question a map asks every time someone draws on it.

This builds the read surface for that question: one document per comparable,
closed sale and active listing in the same collection, with every figure a CMA
needs already computed and a `center` that is always populated. A drawn parcel
is then ONE `find` over stored fields, and the summary is a median over stored
per-item figures. Nothing is derived while someone waits.

`area-cma.py` is the query side and holds no normalisation logic at all. That
is the point: if the reader has to parse a field, the parse belongs here.

── Why a separate collection and not a subdocument ──────────────────────────
`cmaStats` hangs off the listing because it is about that listing. A parcel
query is about a SET of listings spanning two collections, so a subdocument
would force the reader to query `unified_listings` and
`unified_closed_listings` separately and merge them. One collection with a
`status` discriminator makes it a single query, which is what the invariant
asks for. It is also rebuildable and drops without touching the feed — the
cluster is a single node with no backups, and the sync shares it.

── The scalar bbox is not slow by accident, and geo was not an option ───────
Measured on a 2-mile box around Palm Desert: a `latitude`/`longitude` range
query against `latitude_1_longitude_1_closeDate_1_propertyType_1` took
**76 seconds** for 93 rows. A compound B-tree can only range-scan its leading
key, so a 0.029-degree latitude band is scanned across every longitude in
California and longitude is filtered in memory.

The obvious fix — `$geoWithin` against `unified_closed_listings.coordinates`,
which is 2dsphere indexed — silently under-returns: `coordinates` is present on
only **26.5%** of Coachella Valley type-A closings while `latitude`/`longitude`
are on **100%**. The same box returned 469 rows by geo against 532 by scalar.
An index that is complete and an index that is fast were two different indexes,
and neither was both.

So `center` here is built FROM the scalar pair, which makes it complete by
construction, and carries the 2dsphere. Both properties at once, and the
under-return stops being something every caller has to remember.

── Three field names that are empty, and two traps underneath them ──────────
Measured on 6,120 CV type-A closings (trailing 12 months) and 3,376 actives.
The obvious name is empty in each case, so reading it yields a column of blanks
that looks like sparse MLS data rather than a wrong column:

  lotSizeSqft    0.0%  ->  lotSizeArea      98.8%
  daysOnMarket   0.0%  ->  closeDate - onMarketDate
  poolPrivateYN  0.0%  ->  poolFeatures     76.6%

Both replacements have a second trap:

`lotSizeArea` MIXES UNITS. `lotSizeUnits` is populated on 26.5% of rows and
says 'Square Feet' 1,604 times and 'Acres' 16 times; the other 73.5% declare
nothing. Reading the column raw puts 0.2 and 16,117 in the same column and a
median over it is meaningless. Resolution order is `lotSizeSquareFeet` (40.5%,
unambiguous), then `lotSizeAcres` (44.2%, unambiguous), then `lotSizeArea`
disambiguated by `lotSizeUnits` when declared and by magnitude when not. The
magnitude rule is safe here because the distribution is bimodal with an empty
middle: 5,640 rows are >= 100 and 61 are < 10, and only **3 rows in 6,120**
land between. A 10-square-foot lot does not exist and a 10-acre lot is 435,600
square feet, so the two populations cannot be confused. Where both
`lotSizeSquareFeet` and `lotSizeArea` are present they agree on 1,849 rows and
differ by exactly a 43,560 factor on 17 — i.e. `lotSizeArea` was acres there.
`lotSizeSource` records which rule fired so a disagreement stays auditable.

`poolFeatures` HAS TWO SERIALISATIONS. 3,105 rows are comma-joined strings
('Community, In Ground') and 1,580 are dicts ({'in Ground': True,
'private': True}). A parser that assumes a string silently drops a third of the
populated rows. Worse, presence is not a private pool: of the populated rows
2,136 are private, 2,002 are community or association only, and 275 say 'None'.
Treating "has poolFeatures" as "has a pool" calls two thousand condos with a
shared pool pooled homes, which is the single most load-bearing attribute in a
desert comp. `poolKind` is therefore four-valued and 'other' is a real answer,
not a default.

── daysOnMarket is a Mongoose virtual ───────────────────────────────────────
It is 0% populated in Mongo on both collections and is computed in the Node
layer, which is why `build-subdivision-cma.py` already derives it. `onMarketDate`
is an ISO *string* while `closeDate` is a BSON datetime, so the subtraction
needs a coercion on one side and not the other.

── What is deliberately not here ────────────────────────────────────────────
No scoring and no tiering. Those need a subject property to score against, and
a parcel has none — `build-listing-cma.py` is the right tool the moment there
is a subject. Comps are ordered by `completeness` then recency, the same rule
`build-subdivision-cma.py` uses for a subdivision and for the same reason.
`completeness` is stored so that ordering is a sort over a field.

`unified_in_escrow` is excluded, matching the subdivision CMA: a pending deal
is a commitment in progress, not a market observation.
"""

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING, GEOSPHERE, ReplaceOne
from pymongo.errors import OperationFailure

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

DB_NAME = os.getenv("MONGODB_DB", "admin")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

CLOSED_SRC = "unified_closed_listings"
ACTIVE_SRC = "unified_listings"
TARGET = "cma_comps"
META_ID = "cc:meta"

ACTIVE_STATUSES = ["Active", "ComingSoon"]

COACHELLA_VALLEY = [
    "Palm Springs", "Palm Desert", "La Quinta", "Indian Wells",
    "Rancho Mirage", "Cathedral City", "Indio", "Desert Hot Springs",
    "Coachella", "Thermal", "Bermuda Dunes", "Mecca", "Thousand Palms",
    "Sky Valley",
]

ACRE_SQFT = 43560

# The empty middle of the bimodal lotSizeArea distribution. Measured: 3 rows of
# 6,120 land in [10, 100); 61 below, 5,640 above.
LOT_ACRES_MAX = 10.0

# A lot larger than this is a ranch or a data error, not a residential comp.
LOT_SQFT_MAX = 4_356_000      # 100 acres
LIVING_AREA_MIN = 100
DOM_MAX = 3650

BATCH = 2000

PROJECTION = {
    "listingKey": 1, "mlsSource": 1, "mlsId": 1, "propertyType": 1,
    "propertySubType": 1, "standardStatus": 1,
    "closePrice": 1, "listPrice": 1, "originalListPrice": 1,
    "closeDate": 1, "onMarketDate": 1, "listingContractDate": 1,
    "livingArea": 1,
    "lotSizeSquareFeet": 1, "lotSizeAcres": 1, "lotSizeArea": 1,
    "lotSizeUnits": 1,
    "bedsTotal": 1, "bathsTotal": 1, "bathroomsTotalInteger": 1,
    "yearBuilt": 1, "garageSpaces": 1, "view": 1, "stories": 1,
    "poolFeatures": 1, "spaFeatures": 1,
    "latitude": 1, "longitude": 1,
    "city": 1, "postalCode": 1, "subdivisionName": 1, "countyOrParish": 1,
    "unparsedAddress": 1, "streetNumber": 1, "streetName": 1,
    "slug": 1, "slugAddress": 1,
}

# Fields that make a comp usable. completeness is the fraction of these present,
# which is what orders comps when there is no subject to score against.
COMPLETENESS_FIELDS = (
    "price", "livingArea", "pricePerSqft", "lotSizeSqft",
    "beds", "baths", "yearBuilt", "poolKnown", "daysOnMarket",
)


# ── coercion ─────────────────────────────────────────────────────────────────

def num(v):
    """Numbers arrive as int, float, str or Decimal128 depending on the feed."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def coerce_date(v):
    """closeDate is a BSON datetime; onMarketDate is an ISO string."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── the three normalisations ─────────────────────────────────────────────────

def lot_size_sqft(d):
    """Square feet plus the rule that produced it. See the module docstring."""
    sf = num(d.get("lotSizeSquareFeet"))
    if sf and sf > 0:
        return (int(round(sf)), "sqft") if sf <= LOT_SQFT_MAX else (None, "out_of_range")

    ac = num(d.get("lotSizeAcres"))
    if ac and ac > 0:
        v = ac * ACRE_SQFT
        return (int(round(v)), "acres") if v <= LOT_SQFT_MAX else (None, "out_of_range")

    a = num(d.get("lotSizeArea"))
    if a and a > 0:
        units = str(d.get("lotSizeUnits") or "").strip().lower()
        if units.startswith("acre"):
            v, src = a * ACRE_SQFT, "area_declared_acres"
        elif units.replace(" ", "") in ("squarefeet", "sqft", "squarefoot"):
            v, src = a, "area_declared_sqft"
        elif a < LOT_ACRES_MAX:
            v, src = a * ACRE_SQFT, "area_inferred_acres"
        else:
            v, src = a, "area_inferred_sqft"
        return (int(round(v)), src) if v <= LOT_SQFT_MAX else (None, "out_of_range")

    return None, "none"


def feature_tokens(v):
    """poolFeatures / spaFeatures arrive as a string, a dict or a list."""
    if v is None:
        return set()
    if isinstance(v, dict):
        return {str(k).strip().lower() for k, on in v.items() if on}
    if isinstance(v, (list, tuple, set)):
        return {str(x).strip().lower() for x in v if x}
    if isinstance(v, str):
        return {p.strip().lower() for p in v.split(",") if p.strip()}
    return set()


def feature_kind(v):
    """private | community | none | other | unknown — 'other' is a real answer.

    Order matters: 'Community, In Ground, Private' is a home with its own pool
    in a community that also has one, and the private pool is what prices it.
    """
    t = feature_tokens(v)
    if not t:
        return "unknown"
    if "private" in t:
        return "private"
    if t & {"none", "no pool", "no spa"}:
        return "none"
    if t & {"community", "association", "shared", "in ground, community"}:
        return "community"
    return "other"


def street_address(d):
    """`address` is 0% populated; `unparsedAddress` is 99.9%. Same class of
    trap as the three in the module docstring — the obvious name is empty."""
    v = d.get("unparsedAddress")
    if v and str(v).strip():
        return str(v).strip()
    num_, name = d.get("streetNumber"), d.get("streetName")
    if num_ and name:
        return f"{num_} {name}".strip()
    return None


def days_on_market(d, status, now):
    start = coerce_date(d.get("onMarketDate")) or coerce_date(d.get("listingContractDate"))
    if start is None:
        return None, "none"
    end = coerce_date(d.get("closeDate")) if status == "closed" else now
    if end is None:
        return None, "none"
    # Compare dates, not datetimes. onMarketDate carries a wall-clock time and
    # closeDate is midnight, so subtracting the datetimes floors a same-day
    # close to -1 day. In MLS semantics both sides are dates.
    v = (end.date() - start.date()).days
    if v < 0 or v > DOM_MAX:
        return None, "out_of_range"
    return v, "close_minus_onmarket" if status == "closed" else "today_minus_onmarket"


# ── document assembly ────────────────────────────────────────────────────────

def valid_point(lat, lng):
    if lat is None or lng is None:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return False
    return not (abs(lat) < 1e-9 and abs(lng) < 1e-9)


def build_doc(d, status, now):
    """None when the row cannot be placed or priced — both are load-bearing."""
    key = d.get("listingKey")
    if not key:
        return None

    lat, lng = num(d.get("latitude")), num(d.get("longitude"))
    if not valid_point(lat, lng):
        return None

    close_price = num(d.get("closePrice"))
    list_price = num(d.get("listPrice"))
    orig_price = num(d.get("originalListPrice"))

    price = close_price if status == "closed" else list_price
    if not price or price <= 0:
        return None

    if status == "closed":
        event = coerce_date(d.get("closeDate"))
    else:
        event = coerce_date(d.get("onMarketDate")) or coerce_date(d.get("listingContractDate"))
    if event is None:
        return None

    living = num(d.get("livingArea"))
    living = int(round(living)) if living and living >= LIVING_AREA_MIN else None
    ppsf = round(price / living, 2) if living else None

    lot_sqft, lot_src = lot_size_sqft(d)
    dom, dom_src = days_on_market(d, status, now)

    pool = feature_kind(d.get("poolFeatures"))
    spa = feature_kind(d.get("spaFeatures"))

    beds = num(d.get("bedsTotal"))
    baths = num(d.get("bathsTotal"))
    if baths is None:
        baths = num(d.get("bathroomsTotalInteger"))
    year_built = num(d.get("yearBuilt"))
    year_built = int(year_built) if year_built and 1800 < year_built < 2100 else None

    s2l = None
    if status == "closed" and close_price and orig_price and orig_price > 0:
        s2l = round(close_price / orig_price, 4)

    doc = {
        "_id": f"cc:{status[0]}:{key}",
        "listingKey": key,
        "status": status,
        "propertyType": d.get("propertyType"),
        "propertySubType": d.get("propertySubType"),
        "standardStatus": d.get("standardStatus"),
        "mlsSource": d.get("mlsSource"),
        "mlsId": d.get("mlsId"),

        # geometry — built from the scalar pair, so it is never missing
        "center": {"type": "Point", "coordinates": [lng, lat]},
        "lat": lat,
        "lng": lng,

        "price": int(round(price)),
        "pricePerSqft": ppsf,
        "livingArea": living,
        "lotSizeSqft": lot_sqft,
        "lotSizeSource": lot_src,
        "beds": int(beds) if beds is not None else None,
        "baths": float(baths) if baths is not None else None,
        "yearBuilt": year_built,
        "ageAtEvent": (event.year - year_built) if year_built else None,
        "garageSpaces": num(d.get("garageSpaces")),

        "poolKind": pool,
        "hasPrivatePool": pool == "private",
        "spaKind": spa,

        "daysOnMarket": dom,
        "domSource": dom_src,

        "eventDate": event.replace(tzinfo=None),
        "eventYear": event.year,
        "closePrice": int(round(close_price)) if close_price else None,
        "listPrice": int(round(list_price)) if list_price else None,
        "originalListPrice": int(round(orig_price)) if orig_price else None,
        "saleToListRatio": s2l,

        "city": d.get("city"),
        "postalCode": d.get("postalCode"),
        "subdivisionName": d.get("subdivisionName"),
        "county": d.get("countyOrParish"),
        "address": street_address(d),
        "slug": d.get("slugAddress") or d.get("slug"),

        "builtAt": now.replace(tzinfo=None),
    }

    present = {
        "price": doc["price"],
        "livingArea": doc["livingArea"],
        "pricePerSqft": doc["pricePerSqft"],
        "lotSizeSqft": doc["lotSizeSqft"],
        "beds": doc["beds"],
        "baths": doc["baths"],
        "yearBuilt": doc["yearBuilt"],
        "poolKnown": None if pool == "unknown" else pool,
        "daysOnMarket": doc["daysOnMarket"],
    }
    have = sum(1 for f in COMPLETENESS_FIELDS if present[f] is not None)
    doc["completeness"] = round(have / len(COMPLETENESS_FIELDS), 3)

    return doc


# ── scan and write ───────────────────────────────────────────────────────────

def source_filter(status, ptype, cities, since_year):
    q = {"propertyType": ptype}
    if status == "active":
        q["standardStatus"] = {"$in": ACTIVE_STATUSES}
    if cities is not None:
        q["city"] = {"$in": cities}
    if status == "closed" and since_year:
        q["closeDate"] = {"$gte": datetime(since_year, 1, 1)}
    return q


def run_side(status, ptype, cities, since_year, now, dry_run, verbose):
    src = CLOSED_SRC if status == "closed" else ACTIVE_SRC
    q = source_filter(status, ptype, cities, since_year)

    scanned = written = skipped = 0
    stats = Counter()
    lot_src = Counter()
    pool_mix = Counter()
    ops = []
    target = db[TARGET]

    cursor = db[src].find(q, PROJECTION, no_cursor_timeout=True).batch_size(BATCH)
    try:
        for d in cursor:
            scanned += 1
            doc = build_doc(d, status, now)
            if doc is None:
                skipped += 1
                continue
            lot_src[doc["lotSizeSource"]] += 1
            pool_mix[doc["poolKind"]] += 1
            if doc["pricePerSqft"] is not None:
                stats["ppsf"] += 1
            if doc["daysOnMarket"] is not None:
                stats["dom"] += 1
            if not dry_run:
                ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
                if len(ops) >= BATCH:
                    target.bulk_write(ops, ordered=False)
                    written += len(ops)
                    ops = []
            else:
                written += 1
            if verbose and scanned % 50_000 == 0:
                print(f"    {status}: scanned {scanned:,}  written {written:,}")
        if ops and not dry_run:
            target.bulk_write(ops, ordered=False)
            written += len(ops)
    finally:
        cursor.close()

    print(f"  {status:6s} scanned {scanned:,}  written {written:,}  "
          f"unplaced/unpriced {skipped:,}")
    if scanned:
        kept = scanned - skipped
        if kept:
            print(f"         pricePerSqft {stats['ppsf']/kept*100:5.1f}%   "
                  f"daysOnMarket {stats['dom']/kept*100:5.1f}%")
        print(f"         lotSizeSource {dict(lot_src.most_common())}")
        print(f"         poolKind      {dict(pool_mix.most_common())}")

    return written, skipped, lot_src, pool_mix


def sweep_stale(ptype, cities, now, dry_run):
    """Rows this run did not refresh. Scoped by city so a --cv run cannot wipe
    rows outside the valley, the same guard build-area-competition.py uses."""
    q = {"propertyType": ptype, "builtAt": {"$lt": now.replace(tzinfo=None)}}
    if cities is not None:
        q["city"] = {"$in": cities}
    if dry_run:
        return db[TARGET].count_documents(q)
    return db[TARGET].delete_many(q).deleted_count


def ensure_indexes():
    """center_2dsphere is the one that matters: it bounds a query in both
    dimensions, which a compound B-tree on (latitude, longitude) cannot."""
    specs = [
        ("center_2dsphere", [("center", GEOSPHERE)]),
        ("status_type_event", [("status", ASCENDING), ("propertyType", ASCENDING),
                               ("eventDate", DESCENDING)]),
        ("status_type_complete", [("status", ASCENDING), ("propertyType", ASCENDING),
                                  ("completeness", DESCENDING),
                                  ("eventDate", DESCENDING)]),
        ("listingKey_1", [("listingKey", ASCENDING)]),
        ("city_status_event", [("city", ASCENDING), ("status", ASCENDING),
                               ("eventDate", DESCENDING)]),
        ("subdivision_status_event", [("subdivisionName", ASCENDING),
                                      ("status", ASCENDING),
                                      ("eventDate", DESCENDING)]),
        ("zip_status_event", [("postalCode", ASCENDING), ("status", ASCENDING),
                              ("eventDate", DESCENDING)]),
    ]
    for name, keys in specs:
        try:
            db[TARGET].create_index(keys, name=name, background=True)
        except OperationFailure as e:
            print(f"  ⚠️  index {name}: {e}")


def status_report():
    coll = db[TARGET]
    total = coll.count_documents({})
    print("=" * 60)
    print(f"{TARGET}: {total:,} documents")
    print("=" * 60)
    if not total:
        print("  (empty — run the builder)")
        return
    for st in ("closed", "active"):
        n = coll.count_documents({"status": st})
        print(f"  {st:7s} {n:9,}")
    meta = coll.find_one({"_id": META_ID})
    if meta:
        print(f"\n  built {meta.get('builtAt')}  params {meta.get('params')}")
    print("\n  lotSizeSource:")
    for row in coll.aggregate([{"$match": {"_id": {"$ne": META_ID}}},
                               {"$group": {"_id": "$lotSizeSource", "n": {"$sum": 1}}},
                               {"$sort": {"n": -1}}]):
        print(f"    {str(row['_id']):22s} {row['n']:9,}")
    print("\n  poolKind:")
    for row in coll.aggregate([{"$match": {"_id": {"$ne": META_ID}}},
                               {"$group": {"_id": "$poolKind", "n": {"$sum": 1}}},
                               {"$sort": {"n": -1}}]):
        print(f"    {str(row['_id']):22s} {row['n']:9,}")
    print("\n  indexes:")
    for name in db[TARGET].index_information():
        print(f"    {name}")


def main():
    ap = argparse.ArgumentParser(
        description="Build cma_comps — one normalised comparable per document")
    ap.add_argument("--all", action="store_true", help="whole feed")
    ap.add_argument("--cv", action="store_true", help="Coachella Valley only")
    ap.add_argument("--cities", nargs="+", help="explicit city list")
    ap.add_argument("--type", default="A", help="propertyType (A sale, B lease)")
    ap.add_argument("--side", choices=("closed", "active"), action="append",
                    help="limit to one side; repeatable (default: both)")
    ap.add_argument("--since-year", type=int, default=None,
                    help="closings on or after this year (default: all retained)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="keep rows this run did not refresh")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.status:
        status_report()
        return

    if args.cities:
        cities = args.cities
    elif args.cv:
        cities = COACHELLA_VALLEY
    elif args.all:
        cities = None
    else:
        ap.error("pass --all, --cv or --cities")

    sides = args.side or ["closed", "active"]
    now = datetime.now(timezone.utc)
    t0 = time.time()

    print("=" * 60)
    print(f"CMA comps  type {args.type}  sides {', '.join(sides)}")
    print(f"Scope: {'whole feed' if cities is None else ', '.join(cities)}")
    if args.since_year:
        print(f"Closings from: {args.since_year}")
    print("=" * 60)

    if not args.dry_run:
        ensure_indexes()

    total = 0
    for side in sides:
        w, _, _, _ = run_side(side, args.type, cities, args.since_year, now,
                              args.dry_run, args.verbose)
        total += w

    removed = 0
    if not args.no_sweep and set(sides) == {"closed", "active"}:
        removed = sweep_stale(args.type, cities, now, args.dry_run)
    elif not args.no_sweep:
        print("  (stale sweep skipped — a single --side run cannot tell a stale "
              "row from one the other side owns)")

    if not args.dry_run:
        db[TARGET].replace_one(
            {"_id": META_ID},
            {"_id": META_ID, "builtAt": now.replace(tzinfo=None),
             "params": {"propertyType": args.type, "sides": sides,
                        "cities": "all" if cities is None else len(cities),
                        "sinceYear": args.since_year,
                        "lotAcresMax": LOT_ACRES_MAX}},
            upsert=True)

    print("=" * 60)
    print(f"Documents written: {total:,}")
    print(f"Stale removed:     {removed:,}")
    print(f"Elapsed:           {(time.time()-t0)/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    main()
