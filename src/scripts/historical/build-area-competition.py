#!/usr/bin/env python3
"""
Build Area Competition (who sells where, and how few of them there are)

A hot area is only a marketing opportunity if someone can win it. `sales_grid`
and `sales_hotspots` answer "where is it selling"; this answers "who is selling
it, and is that one agent or forty". Those are different questions, and the
second one decides whether the volume is reachable.

One document per area, property type and period, in four scopes. Three of them
(`zip`, `city`, `subdivision`) are keyed exactly like `market_history`, because
those are the units an agent can actually buy a mailing list for. The fourth,
`zone`, is keyed to `sales_hotspots.zoneId` so a map circle can be coloured by
competition from one query instead of a join.

── effectiveAgents is the headline, not agentCount ───────────────────────────
"How many agents sold here" has two answers and only one of them is useful.
`agentCount` is the raw distinct count, and it is dominated by tourists: an area
with 200 sales where one agent did 150 and fifty agents did one each has an
`agentCount` of 51, which reads as a crowded market and is the opposite of the
truth.

`effectiveAgents` is 1/HHI, the Herfindahl numbers-equivalent: the number of
EQUAL-SIZED competitors that would produce the concentration actually observed.
For the area above it is about 1.8, which is what a human would say looking at
the list — this is one agent's farm. One agent at 100% gives exactly 1.0; ten
agents at 10% each give exactly 10.0. It is the literal answer to "dominated by
one agent or by ten".

`salesPerEffectiveAgent` (sales ÷ effectiveAgents, identically sales × HHI) is
the ranking field: it is high only when an area is both busy AND narrowly held.
Sorted descending it lists proven farms with a single incumbent to displace;
sorted ascending it lists fragmented areas with no incumbent at all. Both are
opportunities, of opposite kinds, which is why no "opportunity score" with
invented weights is stored — the sort direction is the thesis, and the caller
picks it.

── Sentinel agents are the correctness guard, not a nicety ───────────────────
Every MLS dumps co-op deals where the other side is not a member into a single
bucket member: "OUT OF AREA - NON MEMBER" and friends. A smoke build of
`build-agents.py` ranked one of these first in the state with 767 deals. Left in
here it would be worse than merely wrong: that bucket accumulates more sales in
an area than any real agent, so it reads as one dominant agent and every area it
touches is mislabelled a one-agent farm. The sentinel sets below are the same
ones `src/scripts/agents/build-agents.py` uses, duplicated rather than imported
because that module loads the environment at import time. `agentCoverage`
reports what fraction of an area's closings survived the guard, so a thin area
cannot pretend to be a measured one.

── Teams count as one competitor ─────────────────────────────────────────────
`listAgentMemberType` is "Teams/Partners" on some rows and the id is the team's.
That is left alone. A team competes for a listing as one unit, so counting it as
one competitor is what the metric is for, not a flaw in it.

── One agent, two MLSs, two ids ─────────────────────────────────────────────
An agent key is `<mlsSource>:<lower(mlsId)>`, identical to `agents._id`, and
`agents` is one document per MLS *registration* — it does not merge an agent who
is a member of two MLSs. So does this: "Dean Sipe" appears in Seven Lakes
Country Club as both `CLAW:44662` and `GPS:51312` with three sales each. The
effect is to SPLIT a cross-registered agent's share, so concentration here is
understated rather than overstated, which is the safe direction for a metric
whose whole job is to find dominance. Merging them needs the identity resolution
`build-agents.py` does (licence number, or exact name plus a shared phone or
brokerage) and belongs there, not in a second implementation here.

── Small samples are biased toward "dominated" ───────────────────────────────
HHI has a floor of 1/n, so an area with 4 sales can never show more than 4
effective agents and will always look concentrated. `--min-sales` (default 10)
is the floor, `quality.confidence` carries the same good/fair/low thresholds as
`market_history`, and anything below `fair` should not be ranked.

── Area scopes count every closing; the zone scope cannot ────────────────────
`latitude` is present on 65.6% of type-A closings. The zip, city and subdivision
scopes do not need it and so count all of them, matching `market_history`
exactly. The `zone` scope is coordinate-bound by definition and covers only the
placeable two thirds. Zone figures are therefore NOT comparable with city
figures over the same ground; `sales` is stored on every document so the
difference is always visible rather than inferred.

── One scan per scope, deliberately ─────────────────────────────────────────
Concentration needs the whole per-agent distribution, not a top-N, so the
accumulator holds an entry per (area, agent, period) triple. Accumulating all
four scopes in one pass runs to millions of entries, which does not fit beside
MongoDB on a 4 GB box with no swap. Each scope gets its own pass instead: four
reads of the collection, with peak memory bounded by the widest single scope.
Peak RSS is printed per scope so a regression here is visible.

Usage:
    python3 src/scripts/historical/build-area-competition.py --cv
    python3 src/scripts/historical/build-area-competition.py --all
    python3 src/scripts/historical/build-area-competition.py --all --scope city
    python3 src/scripts/historical/build-area-competition.py --status
    python3 src/scripts/historical/build-area-competition.py --cv --dry-run --verbose
"""

import os
import re
import math
import time
import argparse
import statistics
import resource
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter

from pymongo import MongoClient, UpdateOne, ASCENDING, DESCENDING, GEOSPHERE
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
SCOPES = ("zone", "subdivision", "city", "zip")
DEFAULT_MIN_SALES = 10
TOP_AGENTS = 10
TOP_BROKERAGES = 5
BULK_BATCH = 500
META_ID = "ac:meta"

# quality.confidence thresholds, identical to market_history
CONF_GOOD = 30
CONF_FAIR = 10

# competitionLevel cut points, stored as a label so the UI renders a bucket it
# did not derive and the two can never disagree.
#
# These are the US DOJ/FTC Horizontal Merger Guidelines thresholds on HHI, not
# numbers chosen here: an HHI under 1,500 is unconcentrated, 1,500-2,500
# moderately concentrated, above 2,500 highly concentrated. They are external
# and citable, which a figure invented for this builder would not be. Whether
# they suit residential listing share specifically is an open question, so
# `ac:meta` also stores the observed distribution of effectiveAgents per scope
# — enough to re-cut them from this data rather than from anyone's judgement.
HHI_MODERATE = 1500
HHI_HIGH = 2500

# A sale is new construction when it closed in the year the home was built or
# the year after. Measured, not assumed, two ways that agree:
#
#   * age-at-close falls off a cliff after 1 — 24.4% of closings at age 0,
#     14.9% at age 1, then 6.1% at age 2 and flat from there.
#   * against the feed's own `newConstructionYN` (87.0% populated, 666,029
#     rows with a true/false value), age <= 1 agrees 98.9% of the time and
#     catches 91.8% of declared-new sales. age <= 0 catches only 60.6%;
#     age <= 2 drops precision from 85.5% to 77.5%.
#
# `yearBuilt` is 99.9% populated against newConstructionYN's 87.0%, and is a
# fact rather than a declaration, so the derived share is the primary figure
# and the declared one is stored beside it as a check.
NEW_BUILD_MAX_AGE = 1

LAT_RANGE = (32.3, 42.1)
LNG_RANGE = (-124.6, -114.0)

PLACEHOLDER_SUBDIVISIONS = {
    "not applicable", "n/a", "none", "other", "na", "no hoa", "see remarks",
    "unknown", "tbd", "various", "not in a development", "no subdivision",
    "-", "--", ".", "",
}
PLACEHOLDER_RE = re.compile(r"^(?:.+:\s*)?other(?:\s*-\s*\d+)?$")

# Same sets as src/scripts/agents/build-agents.py. See the module docstring for
# why these are the most important guard in this builder.
SENTINEL_EXACT = {
    "", "-", "--", "*", "**", ".", "?", "n/a", "na", "none", "null", "nil",
    "unknown", "no name", "tbd", "x", "xx", "xxx", "0",
}
SENTINEL_PHRASES = (
    "out of area", "out of board", "out of town", "out of state",
    "outofarea", "non member", "nonmember", "not a member", "non mls",
    "nonmls", "unknown agent", "unknown office", "unknown broker",
    "no agent", "do not use", "donotuse", "office only", "default agent",
    "non realtor", "nonrealtor", "reciprocal", "non participant",
    "default office", "default agent", "data migration", "datashare",
    "test office", "dummy",
)

COACHELLA_VALLEY = [
    "Palm Springs", "Palm Desert", "La Quinta", "Indian Wells",
    "Rancho Mirage", "Cathedral City", "Indio", "Desert Hot Springs",
    "Coachella", "Thermal", "Bermuda Dunes", "Mecca", "Thousand Palms",
    "Sky Valley",
]

PROJECTION = {
    "city": 1, "postalCode": 1, "subdivisionName": 1, "closeDate": 1,
    "closePrice": 1, "mlsSource": 1, "yearBuilt": 1, "newConstructionYN": 1,
    "listAgentMlsId": 1, "listAgentViewName": 1, "listAgentName": 1,
    "listOfficeName": 1,
    "buyerAgentMlsId": 1, "buyerAgentViewName": 1,
    "latitude": 1, "longitude": 1,
}

# ──────────────────────────────────────────────────────────────────────────────
# 🔌 DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI)
db = client["admin"]
closed_coll = db["unified_closed_listings"]
hist_coll = db["market_history"]
hot_coll = db["sales_hotspots"]
comp_coll = db["area_competition"]


def ensure_indexes():
    """The read routes the analytics tab needs, plus the reverse lookup.

    Called on every non-dry run. An index builder that is defined and never
    called is the same bug as a missing index: every route collection-scans and
    nothing in the output says so.
    """
    comp_coll.create_index(
        [("scope", ASCENDING), ("propertyType", ASCENDING), ("year", ASCENDING),
         ("listSide.salesPerEffectiveAgent", DESCENDING)],
        name="scope_year_sper")
    comp_coll.create_index(
        [("scope", ASCENDING), ("propertyType", ASCENDING), ("year", ASCENDING),
         ("listSide.effectiveAgents", ASCENDING)],
        name="scope_year_effective")
    comp_coll.create_index(
        [("scope", ASCENDING), ("propertyType", ASCENDING), ("year", ASCENDING),
         ("sales", DESCENDING)],
        name="scope_year_sales")
    # "Which dominated areas still have real volume" — the shape of the
    # question the analytics tab is actually for. competitionLevel has to sit
    # before `sales` so the equality prefix leaves the sort indexed.
    comp_coll.create_index(
        [("scope", ASCENDING), ("propertyType", ASCENDING), ("year", ASCENDING),
         ("competitionLevel", ASCENDING), ("sales", DESCENDING)],
        name="scope_year_level_sales")
    # "The most concentrated areas relative to their peers" — the ranking the
    # merger-guideline buckets are too coarse to express.
    comp_coll.create_index(
        [("scope", ASCENDING), ("propertyType", ASCENDING), ("year", ASCENDING),
         ("concentrationPercentile", DESCENDING)],
        name="scope_year_pct")
    comp_coll.create_index(
        [("city", ASCENDING), ("scope", ASCENDING), ("propertyType", ASCENDING),
         ("year", ASCENDING)],
        name="city_scope_year")
    comp_coll.create_index([("zoneId", ASCENDING), ("year", ASCENDING)],
                           name="zone_year")
    # "Where does this agent already dominate", without a scan. This and the
    # 2dsphere below are naturally sparse: a document without the field is
    # simply not indexed, so the zip/city/subdivision rows cost nothing here.
    comp_coll.create_index([("listSide.topAgents.agentId", ASCENDING)],
                           name="top_agent")
    comp_coll.create_index([("center", GEOSPHERE)], name="center_2dsphere")


# ──────────────────────────────────────────────────────────────────────────────
# 🧮 HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def is_placeholder(label):
    s = (label or "").strip().lower()
    return s in PLACEHOLDER_SUBDIVISIONS or bool(PLACEHOLDER_RE.match(s))


def norm_name(s):
    """Lowercase and strip punctuation so a sentinel matches however it is
    spelled. Deliberately lighter than build-agents.py's version, which also
    strips name suffixes: nothing here compares two people's names."""
    if not s:
        return ""
    s = str(s).lower()
    s = re.sub(r"[.,'\"()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_sentinel(s):
    """True for feed placeholders, false for people.

    Phrase-based, not token-based, for the same reason as the original: "Noni
    Area", "Area Pro Realty" and "Boardman Group" are real and must survive.
    """
    n = norm_name(s)
    if n in SENTINEL_EXACT:
        return True
    flat = re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", n)).strip()
    if flat in SENTINEL_EXACT:
        return True
    return any(ph in n or ph in flat for ph in SENTINEL_PHRASES)


# Legal / structural noise stripped only to decide whether two office strings
# are the same brand. Franchise identity is NOT collapsed, exactly as in
# build-agents.py: "Coldwell Banker Realty" and "Coldwell Banker Home Source"
# are different brokerages and stay different competitors.
BROKER_NOISE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c|corp|corporation|co|company|ltd|lp|l\.p|"
    r"incorporated|a\s+california\s+corporation)\b\.?", re.I)


def norm_broker(s):
    """Brand key for a brokerage.

    Offices are keyed by this rather than by `listOfficeMlsId`, because one
    brand holds many office ids: Seven Lakes Country Club listed "Berkshire
    Hathaway Home Services California Properties" twice in `topBrokerages`, at
    27 sales and 3, which is two branches of one competitor and reads as a bug.
    Keying on the brand merges them and makes `effectiveBrokerages` a count of
    firms rather than of storefronts.
    """
    if not s:
        return ""
    s = str(s).lower()
    s = BROKER_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def area_keys(doc, include_placeholders=False):
    """MUST stay identical to area_keys() in build-market-history.py and
    build-area-geo.py. The point of this collection is that its keys join
    theirs; a drift here produces a competition layer with holes in it."""
    out = []
    zp = (doc.get("postalCode") or "").strip()
    city = (doc.get("city") or "").strip()
    sub = (doc.get("subdivisionName") or "").strip()
    if zp:
        out.append(("zip", zp, city or None))
    if city:
        out.append(("city", city.lower(), city))
    if city and sub and (include_placeholders or not is_placeholder(sub)):
        out.append(("subdivision", f"{city.lower()}|{sub.lower()}", sub))
    return out


def confidence(n):
    return "good" if n >= CONF_GOOD else ("fair" if n >= CONF_FAIR else "low")


def competition_level(hhi):
    """The bucket, named once here so no caller re-derives it. Cuts are the
    merger-guideline thresholds; see HHI_MODERATE."""
    if hhi is None:
        return None
    if hhi >= HHI_HIGH:
        return "highly_concentrated"
    if hhi >= HHI_MODERATE:
        return "moderately_concentrated"
    return "unconcentrated"


def deciles(vals):
    """The observed distribution, stored so the cut points above can be checked
    against this data instead of taken on faith."""
    if not vals:
        return None
    s = sorted(vals)
    return {f"p{q}": round(s[min(len(s) - 1, int(len(s) * q / 100))], 2)
            for q in (5, 10, 25, 50, 75, 90, 95)}


def peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def year_coverage():
    """How much of each year the archive actually holds, read from
    market_history rather than recomputed, so one builder owns the answer."""
    cov = {}
    for d in hist_coll.find({}, {"year": 1, "partialYear": 1,
                                 "yearMonthsCovered": 1}):
        y = d.get("year")
        if y is None:
            continue
        cov[y] = (bool(d.get("partialYear")), d.get("yearMonthsCovered"))
    return cov


# ──────────────────────────────────────────────────────────────────────────────
# 📊 CONCENTRATION
# ──────────────────────────────────────────────────────────────────────────────

def concentration(rows, ids, names, with_volume, top_n):
    """Herfindahl, its numbers-equivalent, and the top of the distribution.

    `rows` maps an interned id to [sales] or [sales, volume]; `ids` and `names`
    map that int back to the feed's id and display name. Returns None for an
    empty bucket so no caller divides by zero.

    `hhi` is on the 0-10,000 antitrust scale, because that is the scale every
    published threshold is quoted on. `effectiveAgents` is the same number as
    1/HHI on the 0-1 scale, which expresses it as a count of competitors; the
    module docstring says why that is the one to rank on.

    Ordering is (sales desc, id asc) so a tie cannot reshuffle `topAgents`
    between builds.
    """
    n = sum(r[0] for r in rows.values())
    if n == 0:
        return None

    ordered = sorted(rows.items(), key=lambda kv: (-kv[1][0], ids[kv[0]]))
    hhi = sum((r[0] / n) ** 2 for r in rows.values())
    sales_only = [r[0] for r in rows.values()]
    solo = sum(1 for c in sales_only if c == 1)

    def cum_share(k):
        return sum(r[1][0] for r in ordered[:k]) / n

    top = []
    for key, vals in ordered[:top_n]:
        row = {"id": ids[key], "name": names.get(key), "sales": vals[0],
               "share": round(vals[0] / n, 3)}
        if with_volume:
            row["volume"] = int(vals[1])
        top.append(row)

    return {
        "sales": n,
        "count": len(rows),
        "effective": round(1.0 / hhi, 2),
        "hhi": round(hhi * 10000),
        "salesPerEffective": round(n * hhi, 2),
        "topShare": round(ordered[0][1][0] / n, 3),
        "topSales": ordered[0][1][0],
        "top3Share": round(cum_share(3), 3),
        "top5Share": round(cum_share(5), 3),
        "singleSale": solo,
        "singleSaleShare": round(solo / len(rows), 3),
        "medianPer": round(statistics.median(sales_only), 1),
        "maxPer": max(sales_only),
        "top": top,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🗺️ ZONE MAP
# ──────────────────────────────────────────────────────────────────────────────

def load_zones(ptype, cities):
    """cell -> zoneId, plus the geometry each zone document denormalizes.

    Read from the allYears hot-spot documents, so zone membership here is
    exactly the membership the map draws. `sales_hotspots` clusters once on
    all-time sales and repeats that membership in every year, so there is one
    correct map to read and no year to choose.
    """
    q = {"propertyType": ptype, "allYears": True}
    if cities:
        q["city"] = {"$in": cities}
    cell_zone, geo, cell = {}, {}, None
    for d in hot_coll.find(q, {"zoneId": 1, "memberCellIds": 1, "cell": 1,
                               "center": 1, "lat": 1, "lng": 1,
                               "radiusMiles": 1, "label": 1, "labelKind": 1,
                               "city": 1, "postalCode": 1, "county": 1}):
        zid = d.get("zoneId")
        if not zid:
            continue
        cell = d.get("cell") or cell
        for cid in d.get("memberCellIds") or []:
            parts = cid.split(":")          # "g0.005:<ilat>:<ilng>:<type>"
            if len(parts) != 4:
                continue
            try:
                cell_zone[(int(parts[1]), int(parts[2]))] = zid
            except ValueError:
                continue
        geo[zid] = {
            "center": d.get("center"), "lat": d.get("lat"), "lng": d.get("lng"),
            "radiusMiles": d.get("radiusMiles"),
            "hotspotLabel": d.get("label"), "labelKind": d.get("labelKind"),
            "county": d.get("county"),
        }
    return cell_zone, geo, cell


# ──────────────────────────────────────────────────────────────────────────────
# 🔍 SCAN
# ──────────────────────────────────────────────────────────────────────────────

def scan(scope, ptype, cities, include_placeholders, cell_zone, cell,
         verbose=False):
    """One pass over the closings for ONE scope.

    Accumulates the full per-agent distribution per (area, period), which is
    what HHI needs and what a top-N cannot give. Agent and office ids are
    interned to ints: the accumulator runs to millions of entries and the string
    keys alone would not fit beside the database on this box.
    """
    intern, ids, names = {}, [], {}

    def iid(raw, name):
        k = intern.get(raw)
        if k is None:
            k = len(ids)
            intern[raw] = k
            ids.append(raw)
            if name:
                names[k] = str(name).strip()
        return k

    def blank():
        return {"lst": {}, "off": {}, "buy": {}, "n": 0, "withAgent": 0,
                "cities": Counter(), "zips": Counter(),
                # yearBuilt as a Counter rather than a list: a tract spans a
                # handful of vintages, so this is a few dozen entries per
                # bucket instead of one per sale, and the median is still exact.
                "ybs": Counter(), "newBuild": 0, "declNew": 0, "declN": 0}

    acc = defaultdict(lambda: defaultdict(blank))   # acc[key][period]
    area_label = {}

    q = {"propertyType": ptype}
    if cities:
        q["city"] = {"$in": cities}
    if scope == "zone":
        q["latitude"] = {"$ne": None}
        q["longitude"] = {"$ne": None}

    scanned = placed = 0
    for d in closed_coll.find(q, PROJECTION):
        scanned += 1
        if verbose and scanned % 200_000 == 0:
            print(f"    … {scanned:,} scanned, {peak_mb():.0f} MB peak",
                  flush=True)

        # ── which area(s) does this sale belong to, in THIS scope ──
        if scope == "zone":
            la, ln = d.get("latitude"), d.get("longitude")
            if not isinstance(la, (int, float)) or not isinstance(ln, (int, float)):
                continue
            if not (LAT_RANGE[0] <= la <= LAT_RANGE[1]
                    and LNG_RANGE[0] <= ln <= LNG_RANGE[1]):
                continue
            zid = cell_zone.get((math.floor(la / cell), math.floor(ln / cell)))
            if zid is None:
                continue
            targets = [(zid, None)]
        else:
            targets = [(k, lab) for sc, k, lab in
                       area_keys(d, include_placeholders) if sc == scope]
            if not targets:
                continue
        placed += 1

        cd = d.get("closeDate")
        year = cd.year if isinstance(cd, datetime) else None
        periods = ("all", year) if year is not None else ("all",)

        # ── the two sides, after the sentinel guard ──
        src = (d.get("mlsSource") or "").strip()
        la_id = (d.get("listAgentMlsId") or "").strip()
        la_nm = d.get("listAgentViewName") or d.get("listAgentName")
        lkey = iid(f"{src}:{la_id.lower()}", la_nm) \
            if src and la_id and not is_sentinel(la_id) and not is_sentinel(la_nm) \
            else None

        of_nm = d.get("listOfficeName")
        of_brand = norm_broker(of_nm)
        okey = iid(f"o|{of_brand}", of_nm) \
            if of_brand and not is_sentinel(of_nm) else None

        ba_id = (d.get("buyerAgentMlsId") or "").strip()
        ba_nm = d.get("buyerAgentViewName")
        bkey = iid(f"{src}:{ba_id.lower()}", ba_nm) \
            if src and ba_id and not is_sentinel(ba_id) and not is_sentinel(ba_nm) \
            else None

        price = d.get("closePrice")
        vol = float(price) if isinstance(price, (int, float)) and price > 0 else 0.0
        city = (d.get("city") or "").strip()
        zipc = str(d.get("postalCode") or "").strip()

        yb = d.get("yearBuilt")
        yb = int(yb) if isinstance(yb, (int, float)) and 1800 <= yb <= 2100 else None
        is_new = (yb is not None and year is not None
                  and year - yb <= NEW_BUILD_MAX_AGE)
        decl = d.get("newConstructionYN")
        decl = decl if isinstance(decl, bool) else None

        for key, lab in targets:
            if lab:
                area_label.setdefault(key, lab)
            for period in periods:
                b = acc[key][period]
                b["n"] += 1
                if lkey is not None:
                    b["withAgent"] += 1
                    row = b["lst"].get(lkey)
                    if row is None:
                        b["lst"][lkey] = [1, vol]
                    else:
                        row[0] += 1
                        row[1] += vol
                if okey is not None:
                    row = b["off"].get(okey)
                    if row is None:
                        b["off"][okey] = [1]
                    else:
                        row[0] += 1
                if bkey is not None:
                    row = b["buy"].get(bkey)
                    if row is None:
                        b["buy"][bkey] = [1]
                    else:
                        row[0] += 1
                if city:
                    b["cities"][city] += 1
                if zipc:
                    b["zips"][zipc] += 1
                if yb is not None:
                    b["ybs"][yb] += 1
                    if is_new:
                        b["newBuild"] += 1
                if decl is not None:
                    b["declN"] += 1
                    if decl:
                        b["declNew"] += 1

    print(f"  {scope:12s} scanned {scanned:,}  placed {placed:,}  "
          f"{len(acc):,} areas  {peak_mb():.0f} MB peak", flush=True)
    return acc, ids, names, area_label


# ──────────────────────────────────────────────────────────────────────────────
# 🧱 BUILD
# ──────────────────────────────────────────────────────────────────────────────

def vintage_block(b):
    """Median year built, and how much of the area is new construction.

    `newBuildShare` is derived from `yearBuilt`, which is 99.9% populated and
    is a fact. `declaredNewShare` is the feed's own `newConstructionYN`, which
    is 87.0% populated and is a listing agent's declaration. Both are stored
    because they measure the same thing by different means and disagreeing is
    informative; neither is folded into the other.
    """
    ybn = sum(b["ybs"].values())
    med = None
    if ybn:
        flat = []
        for y, c in sorted(b["ybs"].items()):
            flat.extend([y] * c)
        med = int(statistics.median(flat))
    return {
        "medianYearBuilt": med,
        "yearBuiltCoverage": round(ybn / b["n"], 3) if b["n"] else None,
        "newBuildShare": round(b["newBuild"] / ybn, 3) if ybn else None,
        "newBuildSales": b["newBuild"],
        "declaredNewShare": round(b["declNew"] / b["declN"], 3) if b["declN"] else None,
        "declaredCoverage": round(b["declN"] / b["n"], 3) if b["n"] else None,
        "maxAgeCountedNew": NEW_BUILD_MAX_AGE,
    }


def area_name(scope, key, g, city, area_label):
    """The caption, decided once here. A ZIP is named by its dominant city
    rather than by the first city seen in the scan, because a ZIP that straddles
    two cities would otherwise be captioned by whichever closing came back
    first — which is not stable between builds."""
    if scope == "zone":
        return g.get("hotspotLabel")
    if scope == "zip":
        return f"{city} {key}".strip() if city else key
    return area_label.get(key, key)


def build_docs(scope, acc, ids, names, area_label, geo, ptype, min_sales,
               top_n, coverage, now):
    """One document per (area, period). `_id` is deterministic and carries no
    rank, so nothing goes stale when an area's position moves — unlike
    sales_hotspots, where rank is in the key and a shrinking period leaves
    orphans behind."""
    docs = []
    for key, periods in acc.items():
        for period, b in periods.items():
            if b["n"] < min_sales:
                continue
            lst = concentration(b["lst"], ids, names, True, top_n)
            if lst is None:
                continue
            off = concentration(b["off"], ids, names, False, TOP_BROKERAGES)
            buy = concentration(b["buy"], ids, names, False, top_n)

            is_all = period == "all"
            city = b["cities"].most_common(1)[0][0] if b["cities"] else None
            zipc = b["zips"].most_common(1)[0][0] if b["zips"] else None
            partial, months = (None, None) if is_all \
                else coverage.get(period, (None, None))
            g = geo.get(key, {}) if scope == "zone" else {}

            doc = {
                "_id": f"ac:{scope}:{key}:{ptype}:{'all' if is_all else period}",
                "scope": scope,
                "key": key,
                "label": area_name(scope, key, g, city, area_label),
                "propertyType": ptype,
                "year": None if is_all else period,
                "allYears": is_all,
                # Every closing in the area-period, before the sentinel guard.
                "sales": b["n"],
                # What the concentration figures are actually computed over.
                "salesWithAgent": b["withAgent"],
                "agentCoverage": round(b["withAgent"] / b["n"], 3),
                "listSide": {
                    # The denominator of every share in this subdocument. It is
                    # salesWithAgent, NOT the top-level `sales`: dividing by
                    # `sales` instead silently understates every share by
                    # whatever the sentinel guard removed.
                    "salesCounted": lst["sales"],
                    "agentCount": lst["count"],
                    "effectiveAgents": lst["effective"],
                    "hhi": lst["hhi"],
                    "salesPerEffectiveAgent": lst["salesPerEffective"],
                    "topAgentShare": lst["topShare"],
                    "topAgentSales": lst["topSales"],
                    "top3Share": lst["top3Share"],
                    "top5Share": lst["top5Share"],
                    "singleSaleAgents": lst["singleSale"],
                    # Of AGENTS, not of sales: the fraction of the competitors
                    # here who sold exactly one. High means most of the names on
                    # the list are passing through, not farming.
                    "singleSaleAgentShare": lst["singleSaleShare"],
                    "medianSalesPerAgent": lst["medianPer"],
                    "maxSalesPerAgent": lst["maxPer"],
                    "brokerageCount": off["count"] if off else 0,
                    "effectiveBrokerages": off["effective"] if off else None,
                    "brokerageHhi": off["hhi"] if off else None,
                    "topBrokerageShare": off["topShare"] if off else None,
                    # agentId is agents._id verbatim, so the dashboard can open
                    # the agent record without a name match.
                    "topAgents": [
                        {"agentId": r["id"], "name": r["name"],
                         "sales": r["sales"], "share": r["share"],
                         "volume": r["volume"]}
                        for r in lst["top"]],
                    "topBrokerages": [
                        {"name": r["name"], "sales": r["sales"],
                         "share": r["share"]}
                        for r in (off["top"] if off else [])],
                },
                # The buy side answers a different question — who brings the
                # buyers — and is stored separately because a listing farm and
                # a buyer farm are rarely the same agents.
                "buySide": {
                    # Its own denominator: the buy side loses a different set of
                    # rows to the sentinel guard than the list side does.
                    "salesCounted": buy["sales"],
                    "agentCount": buy["count"],
                    "effectiveAgents": buy["effective"],
                    "hhi": buy["hhi"],
                    "topAgentShare": buy["topShare"],
                    "topAgentSales": buy["topSales"],
                    "topAgents": [
                        {"agentId": r["id"], "name": r["name"],
                         "sales": r["sales"], "share": r["share"]}
                        for r in buy["top"]],
                } if buy else None,
                "competitionLevel": competition_level(lst["hhi"]),
                # One agent holding over half an area's closings is a fact
                # about the data, not a threshold anyone chose.
                "majorityHeld": lst["topShare"] > 0.5,
                # Vintage. A tract where most sales are new builds is held by
                # whoever built it, which is a different thing from a farm an
                # agent won — so the two signals are stored separately rather
                # than folded into one verdict.
                "vintage": vintage_block(b),
                "quality": {"confidence": confidence(b["n"])},
                "city": city,
                "postalCode": zipc,
                "partialYear": partial,
                "yearMonthsCovered": months,
                "builtAt": now,
            }
            if scope == "zone":
                doc.update({
                    "zoneId": key,
                    "center": g.get("center"),
                    "lat": g.get("lat"), "lng": g.get("lng"),
                    "radiusMiles": g.get("radiusMiles"),
                    "labelKind": g.get("labelKind"),
                    "county": g.get("county"),
                })
            docs.append(doc)

    # ── concentrationPercentile: the only ranking with no chosen number in it ──
    # The merger-guideline cuts above are citable but badly placed for this
    # data: measured over this feed they put 89-97% of areas in
    # `unconcentrated`, because they were written for industries with a handful
    # of firms, not for a market with 240,000 licensees. So each area also gets
    # its rank within its OWN scope and period — the share of peer areas whose
    # HHI is at or below its own. 0.99 means more concentrated than 99% of
    # comparable areas, and no threshold is involved.
    #
    # It is relative by construction, so it moves when the population moves.
    # That is the honest behaviour for "how unusual is this", and it is stored
    # rather than computed per request so a read route still never calculates.
    by_period = defaultdict(list)
    for d in docs:
        by_period[(d["propertyType"], d["allYears"], d["year"])].append(d)
    for group in by_period.values():
        group.sort(key=lambda d: (d["listSide"]["hhi"], d["_id"]))
        n = len(group)
        i = 0
        while i < n:
            j = i
            while j + 1 < n and group[j + 1]["listSide"]["hhi"] == group[i]["listSide"]["hhi"]:
                j += 1
            # Ties share one percentile: the whole tied block's upper rank.
            pct = round((j + 1) / n, 3)
            for d in group[i:j + 1]:
                d["concentrationPercentile"] = pct
                d["peerAreas"] = n
            i = j + 1
    return docs


def write_docs(docs, scope, ptype, cities, now, dry_run):
    """Upsert, then drop anything THIS RUN'S SCOPE should have produced and did
    not — an area that fell below `--min-sales`, or a zone that no longer
    exists.

    The sweep is scoped to the cities being rebuilt, because a `--cv` run must
    not wipe the thousands of rows outside the valley that an earlier `--all`
    wrote. There are no database backups to recover them from. Every document
    carries `city` (the dominant city of its sales) precisely so that this
    sweep can be narrowed, including for the zip scope, whose key does not name
    a city.
    """
    if dry_run:
        return 0, 0
    written = 0
    for i in range(0, len(docs), BULK_BATCH):
        chunk = docs[i:i + BULK_BATCH]
        res = comp_coll.bulk_write(
            [UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in chunk],
            ordered=False)
        written += res.upserted_count + res.modified_count
    # Swept on builtAt rather than an `_id: {$nin: [...]}` list. The statewide
    # subdivision scope produces tens of thousands of ids, and shipping all of
    # them back as a query document is both slow and close to the 16 MB BSON
    # limit. Every document rebuilt by this run carries this run's `builtAt`,
    # so anything older is exactly what the run should have produced and did
    # not. `$set` touches builtAt on every upsert, so an unchanged document is
    # still refreshed and survives.
    q = {"scope": scope, "propertyType": ptype, "builtAt": {"$lt": now}}
    if cities:
        q["city"] = {"$in": cities}
    return written, comp_coll.delete_many(q).deleted_count


# ──────────────────────────────────────────────────────────────────────────────
# 📋 STATUS
# ──────────────────────────────────────────────────────────────────────────────

def status():
    total = comp_coll.estimated_document_count()
    print(f"\narea_competition: {total:,} documents")
    if not total:
        return
    meta = comp_coll.find_one({"_id": META_ID})
    if meta:
        print(f"  built {meta.get('builtAt')}  params {meta.get('params')}")

    print("\n  by scope and period:")
    for r in comp_coll.aggregate([
            {"$match": {"_id": {"$ne": META_ID}}},
            {"$group": {"_id": {"s": "$scope", "y": "$year"},
                        "n": {"$sum": 1}}},
            {"$sort": {"_id.s": 1, "_id.y": 1}}]):
        y = r["_id"]["y"] or "all"
        print(f"    {r['_id']['s']:12s} {str(y):>5s}  {r['n']:>6,}")

    print("\n  competitionLevel mix (allYears):")
    for r in comp_coll.aggregate([
            {"$match": {"allYears": True}},
            {"$group": {"_id": {"s": "$scope", "l": "$competitionLevel"},
                        "n": {"$sum": 1}}},
            {"$sort": {"_id.s": 1, "n": -1}}]):
        print(f"    {r['_id']['s']:12s} {str(r['_id']['l']):14s} {r['n']:>6,}")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Build area_competition: agent concentration per area")
    ap.add_argument("--all", action="store_true", help="whole feed")
    ap.add_argument("--cv", action="store_true", help="Coachella Valley only")
    ap.add_argument("--cities", nargs="+", help="explicit city list")
    ap.add_argument("--scope", choices=SCOPES, action="append",
                    help="limit to one scope; repeatable (default: all four)")
    ap.add_argument("--type", default="A", help="propertyType (A sale, B lease)")
    ap.add_argument("--min-sales", type=int, default=DEFAULT_MIN_SALES,
                    help=f"floor per area-period (default {DEFAULT_MIN_SALES})")
    ap.add_argument("--top", type=int, default=TOP_AGENTS,
                    help=f"agents listed per area (default {TOP_AGENTS})")
    ap.add_argument("--include-placeholders", action="store_true",
                    help="keep 'Other' / 'N/A' subdivision labels")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return

    if args.cities:
        cities = args.cities
    elif args.cv:
        cities = COACHELLA_VALLEY
    elif args.all:
        cities = None
    else:
        ap.error("pass --all, --cv or --cities")

    scopes = args.scope or list(SCOPES)
    ptype = args.type
    t0 = time.time()
    now = datetime.now(timezone.utc)
    coverage = year_coverage()

    print("=" * 60)
    print(f"Area competition  type {ptype}  scopes {', '.join(scopes)}")
    print(f"Scope: {'whole feed' if cities is None else ', '.join(cities)}")
    print("=" * 60)

    if not args.dry_run:
        ensure_indexes()

    cell_zone, geo, cell = {}, {}, None
    if "zone" in scopes:
        cell_zone, geo, cell = load_zones(ptype, cities)
        print(f"  zones loaded: {len(geo):,} ({len(cell_zone):,} cells, "
              f"cell {cell})")
        if not geo:
            print("  ⚠️  no sales_hotspots documents for this scope — "
                  "run build-sales-hotspots.py first; skipping zone scope")
            scopes = [s for s in scopes if s != "zone"]

    built = removed = 0
    observed = {}
    for scope in scopes:
        acc, ids, names, area_label = scan(
            scope, ptype, cities, args.include_placeholders, cell_zone, cell,
            args.verbose)
        docs = build_docs(scope, acc, ids, names, area_label, geo, ptype,
                          args.min_sales, args.top, coverage, now)
        w, r = write_docs(docs, scope, ptype, cities, now, args.dry_run)
        built += len(docs)
        removed += r
        allyr = [d for d in docs if d["allYears"]]
        observed[scope] = {
            "areas": len(allyr),
            "effectiveAgents": deciles([d["listSide"]["effectiveAgents"]
                                        for d in allyr]),
            "hhi": deciles([d["listSide"]["hhi"] for d in allyr]),
            "newBuildShare": deciles([d["vintage"]["newBuildShare"]
                                      for d in allyr
                                      if d["vintage"]["newBuildShare"] is not None]),
        }
        print(f"  {scope:12s} {len(docs):,} documents"
              + ("  (dry run)" if args.dry_run else f"  written {w:,}  stale {r:,}"))
        if args.verbose and docs:
            hot = sorted(docs, key=lambda d: -d["listSide"]["salesPerEffectiveAgent"])
            for d in hot[:5]:
                ls = d["listSide"]
                nm = (d["label"] or d["key"])[:34]
                nb = d["vintage"]["newBuildShare"]
                print(f"    {nm:34s} {str(d['year'] or 'all'):>4s}"
                      f"  n={d['sales']:>5,}  agents={ls['agentCount']:>4,}"
                      f"  eff={ls['effectiveAgents']:>6.2f}"
                      f"  top={ls['topAgentShare']:>4.0%}"
                      f"  new={'  n/a' if nb is None else format(nb, '4.0%')}"
                      f"  {d['competitionLevel']}")
        del acc, ids, names, area_label

    if not args.dry_run:
        comp_coll.update_one(
            {"_id": META_ID},
            {"$set": {"builtAt": now, "params": {
                "scopes": scopes, "propertyType": ptype,
                "minSales": args.min_sales, "topN": args.top,
                "includePlaceholders": args.include_placeholders,
                "cityScope": "all" if cities is None else cities,
                "levelCuts": {
                    "field": "listSide.hhi",
                    "moderately_concentrated": HHI_MODERATE,
                    "highly_concentrated": HHI_HIGH,
                    "source": ("US DOJ/FTC Horizontal Merger Guidelines HHI "
                               "thresholds, not chosen for this builder"),
                },
                "newBuildRule": {
                    "field": "closeYear - yearBuilt",
                    "maxAge": NEW_BUILD_MAX_AGE,
                    "evidence": ("age-at-close drops 24.4% / 14.9% / 6.1% at "
                                 "ages 0/1/2; against the feed's own "
                                 "newConstructionYN (666,029 rows with a "
                                 "value) age<=1 agrees 98.9% and catches 91.8% "
                                 "of declared-new, vs 60.6% at age<=0 and "
                                 "77.5% precision at age<=2"),
                },
                "rankField": "listSide.salesPerEffectiveAgent",
                # The distribution this build actually observed, so the cut
                # points above can be re-derived from the data rather than
                # trusted.
                "observed": observed,
                "note": ("effectiveAgents is 1/HHI, the number of equal-sized "
                         "competitors matching the observed concentration. "
                         "Rank by salesPerEffectiveAgent desc for busy areas "
                         "held by few agents, asc for fragmented ones. Sentinel "
                         "'non member' bucket agents are excluded; "
                         "agentCoverage reports how many survived. A high "
                         "vintage.newBuildShare means the holder is whoever "
                         "built it, which is not a farm an agent won."),
            }}},
            upsert=True)

    print("\n" + "=" * 60)
    print(f"Documents built:  {built:,}")
    if not args.dry_run:
        print(f"Stale removed:    {removed:,}")
    else:
        print("Dry-run (no writes)")
    print(f"Peak memory:      {peak_mb():.0f} MB")
    print(f"Elapsed:          {(time.time() - t0) / 60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    main()
