#!/usr/bin/env python3
"""
Build the agent + brokerage database.

Produces two collections that make "who are the agents, where do they work, and
what have they sold" a single indexed lookup instead of a collscan over 1.2M
listings. There is **no agent index on any listing collection** (verified
2026-10-14: unified_listings, unified_closed_listings, unified_in_escrow and
unified_off_market have 64/46/11/11 indexes between them and not one mentions an
agent or office field), so every agent question today is a full scan. That is the
whole reason this builder exists.

    agents       one doc per MLS registration  (_id "<mlsSource>:<lower mlsId>")
    brokerages   one doc per normalized brokerage brand  (_id = slug)

── Identity: why the key is mlsSource + listAgentMlsId ───────────────────────
`listAgentKey` / `listAgentId` is the Spark universal 26-digit member key and is
the obvious choice, but it is only **22-35% populated** (measured 2026-10-14 by
sampling 20k docs per collection). `listAgentMlsId` is **100% populated** on
every collection, on both the list and buyer side. So the grain is the MLS
registration:

    _id = "<mlsSource>:<lower(trim(listAgentMlsId))>"

Lowercased because the same agent appears as both "D78163" and "d84657" styles
and GPS/CRMLS are inconsistent about case. `sparkKey` is carried when present so
a caller can still join to `mlsagents` or the Spark feed.

One human with registrations in two MLSs is therefore two documents. That is
deliberate — merging on name alone would fuse every "Maria Garcia" in Southern
California. Instead `identity.personId` clusters registrations conservatively
(license number, or exact name plus a shared phone or brokerage) and
`identity.alsoKnownAs` lists the sibling documents, so a tool can roll a person
up without the model guessing. See cluster_people().

── The closeDate trap ────────────────────────────────────────────────────────
`unified_closed_listings.closeDate` is **mixed BSON types** (measured
2026-10-14): 1,028,003 date + 84,903 string + 4 missing. Two consequences this
builder has to handle and every other closed-listing reader should too:

  * A plain `{closeDate: {$gte: <datetime>}}` silently drops all 84,903
    string-typed docs, because BSON sorts strings before dates. Those strings
    run to 2026-10-12, so they are current production, not junk.
  * A real TTL index (`closeDate_ttl_5years`, expireAfterSeconds 157,680,000)
    deletes date-typed docs past 5 years but **cannot touch the strings**, which
    reach back to 2020-08-28.

So the window is applied to a *converted* date (`$convert ... to date`, with
onError null) rather than to the raw field, and the conversion failures are
counted and reported rather than dropped quietly. The same scan also throws out
35 corrupt future dates (the worst is closeDate year 5015).

── Window ───────────────────────────────────────────────────────────────────
Rolling 5 years by default (`--years`), which matches both the TTL and the
roster scope that was asked for. An agent is included if they have any active or
in-escrow listing at all, or any closed deal (either side) inside the window.

── Cost shape ───────────────────────────────────────────────────────────────
MongoDB is a remote managed cluster, so the grouping runs server-side and only
grouped rows cross the wire; this VPS has 3 GB of RAM and never holds the full
agent map. Each source is a separate streamed pass that upserts its own
subdocument, so a crashed run resumes by re-running. Stale agents are swept at
the end of an --all run by buildId.

Usage:
    python3 src/scripts/agents/build-agents.py --all
    python3 src/scripts/agents/build-agents.py --all --years 5
    python3 src/scripts/agents/build-agents.py --smoke --verbose     # fast, capped
    python3 src/scripts/agents/build-agents.py --all --skip-closed   # identity only
    python3 src/scripts/agents/build-agents.py --indexes-only
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.errors import OperationFailure
from unidecode import unidecode

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("Missing MONGODB_URI in .env.local")

AGENTS = "agents"
BROKERAGES = "brokerages"
ROSTER = "mlsagents"          # legacy, desert MLS only, not written by this repo
ROSTER_SPARK = "agent_roster"  # written by sync-agents-roster.py, all 8 MLSs

SRC_ACTIVE = "unified_listings"
SRC_ESCROW = "unified_in_escrow"
SRC_CLOSED = "unified_closed_listings"
SRC_OFFMKT = "unified_off_market"

BULK = 500

# $push is unbounded, and one group document must still fit in 16 MB of BSON.
# The out-of-area placeholder alone carried 767 closed deals in a 25k-doc smoke
# sample, which extrapolates past 30,000 over the full collection — enough
# pushed rows to fail the whole aggregation with BSONObjectTooLarge. $firstN
# caps each accumulator instead; `rowsTotal` records the true count so a capped
# document says so rather than quietly under-reporting.
ROW_CAP = 15000
OFFICE_ROW_CAP = 3000
TOP_CITIES = 25
TOP_OFFICES = 10
TOP_AGENTS_PER_BROKERAGE = 25

# propertyType codes, confirmed against unified_listings 2026-10-14:
#   A Residential 47,171 | D Land 17,966 | B Residential Lease 15,951 | C Income 5,036
PT_SALE, PT_LEASE, PT_LAND, PT_INCOME = "A", "B", "D", "C"
PT_LABEL = {PT_SALE: "sale", PT_LEASE: "lease", PT_LAND: "land", PT_INCOME: "income"}

# listOfficeName sentinel: 26,735 of 86,124 active listings (31%) share the single
# value "Unknown Office" under one office id. It is an absence of data, not a
# brokerage, so it never becomes a brokerages doc.
#
# The buyer side adds a second, nastier family. A smoke build ranked
# "OUT OF AREA OUT" of "OUT OF AREA - NON MEMBER" first in the state with 767
# deals and $721M of volume — it is the bucket an MLS dumps every co-op deal
# into when the other agent is not a member, so it accumulates more production
# than any real agent and would top every "best agent" list. Matching these is
# the single most important correctness guard in this builder; see the warning
# about superlatives landing on your worst data in
# docs/strategies/tool-development.md.
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


def _is_sentinel(s) -> bool:
    """True for feed placeholders, false for people.

    Checked against both the normalized name and a variant with hyphens,
    slashes and underscores flattened to spaces, so "NON-MEMBER",
    "NON_MEMBER" and "non member" all match the one phrase. Deliberately
    phrase-based rather than token-based: "Noni Area", "Area Pro Realty",
    "Boardman Group" and "Montana Stateman" are real and must survive.
    """
    n = norm_name(s)
    if n in SENTINEL_EXACT:
        return True
    flat = re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", n)).strip()
    if flat in SENTINEL_EXACT:
        return True
    return any(ph in n or ph in flat for ph in SENTINEL_PHRASES)


# Legal / structural noise stripped only to decide whether two brokerage strings
# are the same brand. Franchise identity is NOT collapsed: "Coldwell Banker
# Realty" and "Coldwell Banker Home Source" are different brokerages and stay
# different docs.
BROKER_NOISE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c|corp|corporation|co|company|ltd|lp|l\.p|"
    r"incorporated|a\s+california\s+corporation)\b\.?", re.I
)

TEAM_TOKENS = ("team", "group", "partners", "realty group", "collective",
               "associates", "properties")


# ── helpers ───────────────────────────────────────────────────────────────────

def coerce_date(v):
    """closeDate and friends are mixed datetime / str — normalize to datetime.

    Same contract as the helper in src/scripts/cma/build-subdivision-cma.py.
    """
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
            d = datetime.fromisoformat(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%m/%d/%Y"):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def slugify(s: str) -> str:
    if not s:
        return ""
    s = unidecode(str(s)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:120]


def norm_name(s: str) -> str:
    """Casefold + strip punctuation/suffixes so names compare across MLSs."""
    if not s:
        return ""
    s = unidecode(str(s)).lower()
    s = re.sub(r"[.,'\"()]", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|mr|mrs|ms|dr)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_broker(s: str) -> str:
    if not s:
        return ""
    s = unidecode(str(s)).lower()
    s = BROKER_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_phone(s) -> str:
    if not s:
        return ""
    d = re.sub(r"\D", "", str(s))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def is_sentinel_name(s) -> bool:
    return _is_sentinel(s)


def is_sentinel_office(s) -> bool:
    return _is_sentinel(s)


def median(vals):
    v = sorted(x for x in vals if isinstance(x, (int, float)) and x > 0)
    if not v:
        return None
    n = len(v)
    mid = n // 2
    return float(v[mid]) if n % 2 else float((v[mid - 1] + v[mid]) / 2)


def rnd(v, p=0):
    if v is None:
        return None
    return round(float(v), p) if p else int(round(float(v)))


def top_n(counter_rows, n, key):
    return sorted(counter_rows, key=key, reverse=True)[:n]


def log(msg, *, verbose=True):
    if verbose:
        print(msg, flush=True)


# ── aggregation building blocks ───────────────────────────────────────────────

def _agent_id_expr(field: str):
    """"<mlsSource>:<lower(trim(mlsId))>" — the document grain."""
    return {"$concat": [
        "$mlsSource", ":",
        {"$toLower": {"$trim": {"input": {"$toString": field}}}},
    ]}


def _listside_role():
    return {
        "r": "list",
        "a": "$listAgentMlsId",
        "ak": "$listAgentKey",
        "nm": "$listAgentName",
        "vn": "$listAgentViewName",
        "mn": "$listAgentMarketingName",
        "fn": "$listAgentFirstName",
        "ln": "$listAgentLastName",
        "ph": "$listAgentPreferredPhone",
        "aor": "$listAgentAOR",
        "asn": "$listAgentAssociation",
        "mt": "$listAgentMemberType",
        "oid": "$listOfficeId",
        "om": "$listOfficeMlsId",
        "on": "$listOfficeName",
        "op": "$listOfficePhone",
        "oa": "$listOfficeAOR",
    }


def _buyside_role():
    """The buyer side carries no key, no first/last name and no phone.

    Measured on unified_closed_listings 2026-10-14: buyerAgentMlsId 99.9%,
    buyerAgentViewName 99.9%, buyerOfficeViewName 100%, buyerAgentAOR 80.7%.
    Everything else in the list-side shape simply does not exist, so those slots
    are null and coverage.confidence records the thinner identity.
    """
    return {
        "r": "buy",
        "a": "$buyerAgentMlsId",
        "ak": None,
        "nm": "$buyerAgentViewName",
        "vn": "$buyerAgentViewName",
        "mn": "$buyerAgentMarketingName",
        "fn": None,
        "ln": None,
        "ph": None,
        "aor": "$buyerAgentAOR",
        "asn": "$buyerAgentAssociation",
        "mt": "$buyerAgentMemberType",
        "oid": None,
        "om": "$buyerOfficeMlsId",
        "on": "$buyerOfficeViewName",
        "op": None,
        "oa": "$buyerOfficeAOR",
    }


def _identity_accumulators():
    """Carry the richest identity we saw, plus every variant for provenance."""
    return {
        "sparkKey":   {"$max": "$_r.ak"},
        "nameAny":    {"$max": "$_r.nm"},
        "viewAny":    {"$max": "$_r.vn"},
        "mktAny":     {"$max": "$_r.mn"},
        "firstAny":   {"$max": "$_r.fn"},
        "lastAny":    {"$max": "$_r.ln"},
        "aorAny":     {"$max": "$_r.aor"},
        "assocAny":   {"$max": "$_r.asn"},
        "mlsIdRaw":   {"$max": "$_r.a"},
        "mlsIdVariants": {"$addToSet": "$_r.a"},
        "names":      {"$addToSet": "$_r.nm"},
        "phones":     {"$addToSet": "$_r.ph"},
        "memberTypes": {"$addToSet": "$_r.mt"},
        "officeRows": {"$firstN": {"n": OFFICE_ROW_CAP, "input": {
            "id": "$_r.oid", "mlsId": "$_r.om", "name": "$_r.on",
            "phone": "$_r.op", "aor": "$_r.oa",
        }}},
    }


def _live_pipeline(limit=None, lease_ok=True):
    """Active / in-escrow / off-market: list side only (buyer fields are ~0-3%)."""
    pipe = [
        {"$match": {"listAgentMlsId": {"$nin": [None, ""]},
                    "mlsSource": {"$nin": [None, ""]}}},
    ]
    if limit:
        pipe.append({"$limit": limit})
    pipe += [
        {"$set": {"_r": _listside_role()}},
        {"$group": {
            "_id": _agent_id_expr("$listAgentMlsId"),
            "mlsSource": {"$first": "$mlsSource"},
            "total": {"$sum": 1},
            "volume": {"$sum": {"$ifNull": ["$listPrice", 0]}},
            "prices": {"$push": "$listPrice"},
            "doms": {"$push": "$daysOnMarket"},
            "ptypes": {"$push": "$propertyType"},
            "cities": {"$push": "$city"},
            "zips": {"$push": "$postalCode"},
            "counties": {"$addToSet": "$countyOrParish"},
            "onMarketMin": {"$min": "$onMarketDate"},
            "onMarketMax": {"$max": "$onMarketDate"},
            "lastTouch": {"$max": "$modificationTimestamp"},
            **_identity_accumulators(),
        }},
    ]
    return pipe


def _closed_pipeline(cutoff, ceiling, limit=None):
    """One scan over closed listings emitting BOTH sides.

    The two-element `_roles` array plus $unwind means the 1.1M-doc collection is
    read once instead of twice; grouping then happens on 2.2M role rows, which
    the server spills to disk if needed (allowDiskUse).
    """
    pipe = [
        {"$match": {"mlsSource": {"$nin": [None, ""]},
                    "$or": [{"listAgentMlsId": {"$nin": [None, ""]}},
                            {"buyerAgentMlsId": {"$nin": [None, ""]}}]}},
        # Normalize the mixed-type closeDate BEFORE filtering. See module docstring.
        {"$set": {"_cd": {"$convert": {"input": "$closeDate", "to": "date",
                                       "onError": None, "onNull": None}}}},
        {"$match": {"_cd": {"$gte": cutoff, "$lte": ceiling}}},
    ]
    if limit:
        pipe.append({"$limit": limit})
    pipe += [
        {"$set": {"_roles": [_listside_role(), _buyside_role()]}},
        {"$unwind": "$_roles"},
        {"$set": {"_r": "$_roles"}},
        {"$match": {"_r.a": {"$nin": [None, ""]}}},
        {"$group": {
            "_id": _agent_id_expr("$_r.a"),
            "mlsSource": {"$first": "$mlsSource"},
            "rowsTotal": {"$sum": 1},
            "rows": {"$firstN": {"n": ROW_CAP, "input": {
                "r": "$_r.r",
                "p": "$closePrice",
                "lp": "$listPrice",
                "d": "$_cd",
                "c": "$city",
                "z": "$postalCode",
                "t": "$propertyType",
                "dom": "$daysOnMarket",
            }}},
            "counties": {"$addToSet": "$countyOrParish"},
            **_identity_accumulators(),
        }},
    ]
    return pipe


# ── pass runners ──────────────────────────────────────────────────────────────

def _office_summary(rows):
    """Collapse the pushed office rows into a primary office + a history."""
    seen = {}
    unknown = 0
    for r in rows or []:
        name = r.get("name")
        if is_sentinel_office(name):
            unknown += 1
            continue
        mls_id = (r.get("mlsId") or "").strip()
        key = mls_id.lower() or norm_broker(name)
        if not key:
            unknown += 1
            continue
        e = seen.setdefault(key, {"mlsId": mls_id or None, "id": r.get("id"),
                                  "name": name, "phone": r.get("phone"),
                                  "aor": r.get("aor"), "listings": 0})
        e["listings"] += 1
        for f in ("phone", "aor", "id"):
            if not e.get(f) and r.get(f):
                e[f] = r[f]
    hist = top_n(list(seen.values()), TOP_OFFICES, key=lambda x: x["listings"])
    primary = hist[0] if hist else None
    return primary, hist, unknown


def _identity_patch(doc):
    """Build the identity/office/brokerage fields shared by every pass."""
    names = [n for n in (doc.get("names") or []) if n and not is_sentinel_name(n)]
    best = doc.get("nameAny") or doc.get("viewAny") or doc.get("mktAny")
    if is_sentinel_name(best):
        best = next(iter(names), None)

    phones = sorted({p for p in (norm_phone(x) for x in (doc.get("phones") or [])) if p})
    mts = [m for m in (doc.get("memberTypes") or []) if m]
    primary_office, office_hist, unknown_offices = _office_summary(doc.get("officeRows"))

    mls_id_raw = (doc.get("mlsIdRaw") or "").strip()
    nn = norm_name(best)
    is_team = any(t in nn for t in TEAM_TOKENS) or any(
        "team" in str(m).lower() or "partner" in str(m).lower() for m in mts)

    ident = {
        "name": best,
        "nameNormalized": nn,
        "firstName": doc.get("firstAny"),
        "lastName": doc.get("lastAny"),
        "viewName": doc.get("viewAny"),
        "marketingName": doc.get("mktAny"),
        "nameVariants": sorted(set(names))[:8],
        "phone": phones[0] if phones else None,
        "phones": phones,
        "memberType": mts[0] if mts else None,
        "memberTypes": sorted(set(mts))[:6],
        "isTeam": bool(is_team),
        "aor": doc.get("aorAny"),
        "association": doc.get("assocAny"),
    }

    variants = sorted({str(v).strip() for v in (doc.get("mlsIdVariants") or [])
                       if v not in (None, "")})
    patch = {
        "agentMlsId": mls_id_raw or None,
        "agentMlsIdVariants": variants,
        "agentKey": (doc["_id"].split(":", 1)[1] if ":" in doc["_id"] else None),
        "mlsSource": doc.get("mlsSource"),
        "sparkKey": doc.get("sparkKey"),
        "identity": ident,
    }
    if primary_office:
        bname = primary_office.get("name")
        patch["office"] = {
            "id": primary_office.get("id"),
            "mlsId": primary_office.get("mlsId"),
            "name": bname,
            "phone": primary_office.get("phone"),
            "aor": primary_office.get("aor"),
            "slug": slugify(bname),
        }
        patch["brokerage"] = {
            "name": bname,
            "slug": slugify(norm_broker(bname)) or slugify(bname),
            "normalizedName": norm_broker(bname),
        }
    patch["offices"] = office_hist
    patch["_unknownOfficeListings"] = unknown_offices
    return patch


def _live_stats(doc):
    prices = [p for p in (doc.get("prices") or []) if isinstance(p, (int, float)) and p > 0]
    doms = [d for d in (doc.get("doms") or []) if isinstance(d, (int, float)) and d >= 0]
    pt = Counter(t for t in (doc.get("ptypes") or []) if t)
    cities = Counter(c for c in (doc.get("cities") or []) if c)
    zips = Counter(z for z in (doc.get("zips") or []) if z)

    by_pt = {PT_LABEL.get(k, str(k).lower()): v for k, v in pt.items()}
    return {
        "total": doc.get("total", 0),
        "sale": by_pt.get("sale", 0),
        "lease": by_pt.get("lease", 0),
        "land": by_pt.get("land", 0),
        "income": by_pt.get("income", 0),
        "byPropertyType": by_pt,
        "volume": rnd(doc.get("volume") or 0),
        "medianListPrice": rnd(median(prices)),
        "minListPrice": rnd(min(prices)) if prices else None,
        "maxListPrice": rnd(max(prices)) if prices else None,
        "medianDaysOnMarket": rnd(median(doms)),
        "oldestOnMarketDate": coerce_date(doc.get("onMarketMin")),
        "newestOnMarketDate": coerce_date(doc.get("onMarketMax")),
        "byCity": [{"city": c, "slug": slugify(c), "count": n}
                   for c, n in cities.most_common(TOP_CITIES)],
        "cityCount": len(cities),
    }, cities, zips


def _closed_stats(doc, window_years, cutoff, now):
    rows = doc.get("rows") or []
    total_rows = doc.get("rowsTotal") or len(rows)
    capped = total_rows > len(rows)
    twelve = now - timedelta(days=365)

    side = {"list": [], "buy": []}
    by_year = defaultdict(lambda: {"deals": 0, "volume": 0.0, "listSide": 0, "buySide": 0})
    cities = Counter()
    zips = Counter()
    l12 = {"deals": 0, "volume": 0.0, "listSide": 0, "buySide": 0}
    last_date = None

    for r in rows:
        d = coerce_date(r.get("d"))
        p = r.get("p") if isinstance(r.get("p"), (int, float)) else None
        role = r.get("r")
        side.setdefault(role, []).append(r)
        if r.get("c"):
            cities[r["c"]] += 1
        if r.get("z"):
            zips[r["z"]] += 1
        if d:
            if last_date is None or d > last_date:
                last_date = d
            y = d.year
            by_year[y]["deals"] += 1
            by_year[y]["volume"] += p or 0
            by_year[y]["listSide" if role == "list" else "buySide"] += 1
            if d >= twelve:
                l12["deals"] += 1
                l12["volume"] += p or 0
                l12["listSide" if role == "list" else "buySide"] += 1

    def side_block(rs):
        prices = [r["p"] for r in rs if isinstance(r.get("p"), (int, float)) and r["p"] > 0]
        doms = [r["dom"] for r in rs if isinstance(r.get("dom"), (int, float)) and r["dom"] >= 0]
        pt = Counter(r.get("t") for r in rs if r.get("t"))
        ratios = [r["p"] / r["lp"] for r in rs
                  if isinstance(r.get("p"), (int, float)) and r["p"] > 0
                  and isinstance(r.get("lp"), (int, float)) and r["lp"] > 0
                  and 0.5 <= r["p"] / r["lp"] <= 1.5]
        return {
            "deals": len(rs),
            "volume": rnd(sum(prices)),
            "medianClosePrice": rnd(median(prices)),
            "minClosePrice": rnd(min(prices)) if prices else None,
            "maxClosePrice": rnd(max(prices)) if prices else None,
            "medianDaysOnMarket": rnd(median(doms)),
            "medianSaleToListRatio": rnd(median(ratios), 4) if ratios else None,
            "sale": pt.get(PT_SALE, 0),
            "lease": pt.get(PT_LEASE, 0),
            "land": pt.get(PT_LAND, 0),
            "income": pt.get(PT_INCOME, 0),
        }

    ls, bs = side_block(side.get("list", [])), side_block(side.get("buy", []))
    years = [{"year": y, **{k: (rnd(v) if k == "volume" else v) for k, v in d.items()}}
             for y, d in sorted(by_year.items(), reverse=True)]

    block = {
        "windowYears": window_years,
        "windowStart": cutoff,
        "listSide": ls,
        "buySide": bs,
        "total": {"deals": ls["deals"] + bs["deals"],
                  "volume": rnd((ls["volume"] or 0) + (bs["volume"] or 0))},
        "last12mo": {**l12, "volume": rnd(l12["volume"])},
        "byYear": years,
        "byCity": [{"city": c, "slug": slugify(c), "deals": n}
                   for c, n in cities.most_common(TOP_CITIES)],
        "cityCount": len(cities),
        "lastCloseDate": last_date,
        "dealRowsConsidered": len(rows),
        "dealRowsTotal": total_rows,
        "statsTruncated": capped,
    }
    if capped:
        block["truncationNote"] = (
            f"This record has {total_rows:,} closed-deal rows in the window; "
            f"the breakdowns and medians above are computed from {len(rows):,} "
            f"of them (cap {ROW_CAP:,}). Counts in total/listSide/buySide are "
            f"also from that sample. A figure this large almost always means an "
            f"MLS placeholder id rather than one agent — check "
            f"identity.isSentinel.")
    return block, cities, zips


def run_live_pass(db, coll, field, build_id, *, limit=None, verbose=False, dry=False):
    """Stream one live collection into agents.<field>."""
    t0 = time.time()
    pipe = _live_pipeline(limit=limit)
    cur = db[coll].aggregate(pipe, allowDiskUse=True, batchSize=200)
    ops, n = [], 0
    for doc in cur:
        if not doc["_id"] or doc["_id"].endswith(":"):
            continue
        stats, cities, zips = _live_stats(doc)
        patch = _identity_patch(doc)
        set_doc = {
            field: stats,
            "buildId": build_id,
            f"_cities.{field}": [[c, n2] for c, n2 in cities.most_common(60)],
            f"_zips.{field}": [[z, n2] for z, n2 in zips.most_common(30)],
            f"_counties.{field}": [c for c in (doc.get("counties") or []) if c][:8],
            f"_touch.{field}": coerce_date(doc.get("lastTouch")),
            f"_unknownOffice.{field}": patch.pop("_unknownOfficeListings", 0),
            f"_identity.{field}": patch,
        }
        ops.append(UpdateOne({"_id": doc["_id"]},
                             {"$set": set_doc, "$addToSet": {"coverageSources": coll}},
                             upsert=True))
        n += 1
        if len(ops) >= BULK and not dry:
            db[AGENTS].bulk_write(ops, ordered=False)
            ops = []
    if ops and not dry:
        db[AGENTS].bulk_write(ops, ordered=False)
    log(f"  {coll:26s} -> agents.{field:9s} {n:>8,} agents   {time.time()-t0:6.1f}s",
        verbose=True)
    return n


def run_closed_pass(db, build_id, cutoff, ceiling, window_years, now,
                    *, limit=None, verbose=False, dry=False):
    t0 = time.time()
    pipe = _closed_pipeline(cutoff, ceiling, limit=limit)
    cur = db[SRC_CLOSED].aggregate(pipe, allowDiskUse=True, batchSize=100)
    ops, n = [], 0
    for doc in cur:
        if not doc["_id"] or doc["_id"].endswith(":"):
            continue
        block, cities, zips = _closed_stats(doc, window_years, cutoff, now)
        patch = _identity_patch(doc)
        set_doc = {
            "closed": block,
            "buildId": build_id,
            "_cities.closed": [[c, n2] for c, n2 in cities.most_common(60)],
            "_zips.closed": [[z, n2] for z, n2 in zips.most_common(30)],
            "_counties.closed": [c for c in (doc.get("counties") or []) if c][:8],
            "_touch.closed": block.get("lastCloseDate"),
            "_unknownOffice.closed": patch.pop("_unknownOfficeListings", 0),
            "_identity.closed": patch,
        }
        ops.append(UpdateOne({"_id": doc["_id"]},
                             {"$set": set_doc,
                              "$addToSet": {"coverageSources": SRC_CLOSED}},
                             upsert=True))
        n += 1
        if len(ops) >= BULK and not dry:
            db[AGENTS].bulk_write(ops, ordered=False)
            ops = []
    if ops and not dry:
        db[AGENTS].bulk_write(ops, ordered=False)
    log(f"  {SRC_CLOSED:26s} -> agents.closed    {n:>8,} agents   {time.time()-t0:6.1f}s",
        verbose=True)
    return n


# ── compose pass ──────────────────────────────────────────────────────────────

IDENT_PRIORITY = ("active", "escrow", "offMarket", "closed")


def _merge_identity(doc):
    """Pick the canonical identity from whichever source knows the most.

    Active listings win over closed, and the list side wins over the buyer side,
    because the buyer side has no first/last name and no phone at all. Anything
    a weaker source knows and a stronger one does not is still filled in.
    """
    parts = doc.get("_identity") or {}
    ordered = [parts[k] for k in IDENT_PRIORITY if parts.get(k)]
    if not ordered:
        return None, None, None, None, []

    base = {}
    ident = {}
    office = None
    brokerage = None
    offices = {}
    used = []

    for p in ordered:
        used.append(p)
        for k, v in p.items():
            if k in ("identity", "office", "brokerage", "offices"):
                continue
            if k == "agentMlsIdVariants":
                base[k] = sorted(set(base.get(k, [])) | set(v or []))
                continue
            if base.get(k) in (None, "") and v not in (None, ""):
                base[k] = v
        for k, v in (p.get("identity") or {}).items():
            cur = ident.get(k)
            if isinstance(v, list):
                merged = list(dict.fromkeys((cur or []) + v))
                if merged:
                    ident[k] = merged[:8]
            elif cur in (None, "", False) and v not in (None, ""):
                ident[k] = v
        if office is None and p.get("office"):
            office = p["office"]
            brokerage = p.get("brokerage")
        for o in p.get("offices") or []:
            key = (o.get("mlsId") or norm_broker(o.get("name"))) or ""
            if not key:
                continue
            e = offices.setdefault(key.lower(), dict(o))
            e["listings"] = max(e.get("listings", 0), o.get("listings", 0))

    office_hist = top_n(list(offices.values()), TOP_OFFICES, key=lambda x: x.get("listings", 0))
    return base, ident, office, brokerage, office_hist


def _merge_locations(doc):
    buckets = doc.get("_cities") or {}
    per_city = defaultdict(lambda: defaultdict(int))
    for src, rows in buckets.items():
        for row in rows or []:
            if not row or not row[0]:
                continue
            per_city[row[0]][src] += int(row[1] or 0)

    cities = []
    for city, srcs in per_city.items():
        total = sum(srcs.values())
        cities.append({
            "city": city, "slug": slugify(city),
            "active": srcs.get("active", 0),
            "escrow": srcs.get("escrow", 0),
            "closed": srcs.get("closed", 0),
            "offMarket": srcs.get("offMarket", 0),
            "total": total,
        })
    cities.sort(key=lambda c: (-c["total"], c["city"]))

    zbuckets = doc.get("_zips") or {}
    per_zip = Counter()
    for rows in zbuckets.values():
        for row in rows or []:
            if row and row[0]:
                per_zip[str(row[0])[:5]] += int(row[1] or 0)

    counties = []
    for vals in (doc.get("_counties") or {}).values():
        for c in vals or []:
            if c and c not in counties:
                counties.append(c)

    primary = cities[0] if cities else None
    label = None
    if cities:
        head = [c["city"] for c in cities[:3]]
        label = ", ".join(head) + (f" +{len(cities)-3}" if len(cities) > 3 else "")

    return {
        "primaryCity": primary["city"] if primary else None,
        "primaryCitySlug": primary["slug"] if primary else None,
        "primaryPostalCode": per_zip.most_common(1)[0][0] if per_zip else None,
        "counties": counties[:8],
        "cities": cities[:TOP_CITIES],
        "cityCount": len(cities),
        "postalCodes": [{"postalCode": z, "count": n} for z, n in per_zip.most_common(15)],
        "marketLabel": label,
    }


def _activity(doc, now):
    touches = [coerce_date(v) for v in (doc.get("_touch") or {}).values()]
    touches = [t for t in touches if t]
    last = max(touches) if touches else None
    active = (doc.get("active") or {}).get("total", 0)
    escrow = (doc.get("escrow") or {}).get("total", 0)

    if active or escrow:
        status = "active"
    elif last and last >= now - timedelta(days=548):
        status = "recent"
    else:
        status = "dormant"

    src = None
    if active:
        src = SRC_ACTIVE
    elif escrow:
        src = SRC_ESCROW
    elif (doc.get("closed") or {}).get("total", {}).get("deals"):
        src = SRC_CLOSED
    return {
        "status": status,
        "lastActivityDate": last,
        "lastActivitySource": src,
        "hasLiveInventory": bool(active or escrow),
    }


def _production(doc):
    cl = doc.get("closed") or {}
    ls, bs = cl.get("listSide") or {}, cl.get("buySide") or {}
    act = doc.get("active") or {}
    esc = doc.get("escrow") or {}
    deals = (cl.get("total") or {}).get("deals", 0)
    vol = (cl.get("total") or {}).get("volume", 0) or 0

    ld, bd = ls.get("deals", 0), bs.get("deals", 0)
    if ld and bd:
        role = "both" if min(ld, bd) / max(ld, bd) >= 0.25 else (
            "listing" if ld > bd else "buyer")
    elif ld:
        role = "listing"
    elif bd:
        role = "buyer"
    else:
        role = None

    return {
        "windowDeals": deals,
        "windowVolume": rnd(vol),
        "last12moDeals": (cl.get("last12mo") or {}).get("deals", 0),
        "last12moVolume": (cl.get("last12mo") or {}).get("volume", 0),
        "listSideDeals": ld,
        "buySideDeals": bd,
        "primaryRole": role,
        "activeListings": act.get("total", 0),
        "activeInventoryValue": act.get("volume", 0),
        "inEscrowListings": esc.get("total", 0),
        "avgDealPrice": rnd(vol / deals) if deals else None,
    }


def _coverage(doc, base, ident, office, roster_hit, now):
    srcs = sorted(set(doc.get("coverageSources") or []))
    unknown = sum((doc.get("_unknownOffice") or {}).values())
    counted = ((doc.get("active") or {}).get("total", 0)
               + (doc.get("escrow") or {}).get("total", 0)
               + (doc.get("offMarket") or {}).get("total", 0)
               + ((doc.get("closed") or {}).get("total") or {}).get("deals", 0))

    notes = []
    name = (ident or {}).get("name")
    buy_only = srcs == [SRC_CLOSED] and not (ident or {}).get("firstName")

    if not name or is_sentinel_name(name):
        conf = "low"
        notes.append("No usable agent name in the feed for this MLS id.")
    elif buy_only and counted < 3:
        conf = "low"
        notes.append(
            "Known only as a buyer's agent on closed sales, where the feed "
            "carries no first/last name, phone or office id — identity is a "
            "display name only.")
    elif not office:
        conf = "medium"
        notes.append(
            "Brokerage unknown: every listing for this agent reports the "
            "'Unknown Office' sentinel, which is 31% of active listings "
            "feed-wide and means absent data, not a real brokerage.")
    elif counted < 3:
        conf = "medium"
        notes.append("Fewer than 3 listings/deals in the window — thin sample.")
    else:
        conf = "high"

    if unknown:
        notes.append(f"{unknown} of this agent's listings report no office.")

    return {
        "sources": srcs,
        "listingsCounted": counted,
        "officeKnown": bool(office),
        "officeUnknownListings": unknown,
        "rosterMatched": bool(roster_hit),
        "contactFromRoster": bool(roster_hit and (
            (ident or {}).get("email") or (ident or {}).get("licenseNumber"))),
        "buySideIdentityOnly": bool(buy_only),
        "confidence": conf,
        "notes": notes,
    }


def load_roster(db, verbose=False):
    """Load the member rosters that supply contact detail.

    Two sources, in priority order:

    1. `agent_roster` — all eight MLSs, 158,667 members, written by
       sync-agents-roster.py from Spark /v1/accounts. Joined on
       `agentRef` == this collection's `_id`, i.e.
       "<mlsSource>:<lower(ShortId)>", because ShortId is the same per-MLS
       short code that listings carry in listAgentMlsId.
    2. `mlsagents` — the legacy 4,720-doc desert-only roster, keyed by
       `sparkId` == listAgentKey. Kept as a fallback and never allowed to
       overwrite source 1; nothing in this repo writes it, so it is treated
       as read-only.

    Joining on the Spark 26-digit id alone would be a mistake: it corresponds
    to `sparkKey`, which is 22-35% populated, and matches nothing at all for
    CRMLS, HIGH_DESERT, BRIDGE or ITECH.

    Memory: a tight projection over 158,667 docs is roughly 110 MB of Python
    dicts, which this 3 GB box can hold. If the roster grows much past that,
    switch compose_pass to batched $in lookups instead of a preloaded map.
    """
    FIELDS = {
        "_id": 0, "agentRef": 1, "sparkId": 1, "shortId": 1, "licenseNumber": 1,
        "email": 1, "emails": 1, "phone": 1, "phones": 1, "photoUrl": 1,
        "bio": 1, "name": 1, "active": 1, "officeId": 1, "website": 1,
        "associations": 1, "designations": 1, "languagesSpoken": 1,
        "officeCity": 1, "localType": 1, "marketingName": 1, "mlsSource": 1,
    }

    by_ref, by_spark, by_namephone = {}, {}, {}
    n_spark = 0
    try:
        for r in db[ROSTER_SPARK].find({}, FIELDS):
            n_spark += 1
            ref = r.get("agentRef")
            if ref:
                by_ref[ref] = r
            sid = r.get("sparkId")
            if sid:
                by_spark.setdefault(str(sid), r)
            ph, nn = norm_phone(r.get("phone")), norm_name(r.get("name"))
            if nn and ph:
                by_namephone.setdefault((nn, ph), r)
    except OperationFailure as e:
        log(f"  ! {ROSTER_SPARK} unreadable ({str(e)[:70]}) — falling back",
            verbose=True)

    n_legacy = 0
    for r in db[ROSTER].find({}, {
            "sparkId": 1, "email": 1, "emailsAll": 1, "licenseNumber": 1,
            "phone": 1, "photoUrl": 1, "bio": 1, "name": 1, "isTeam": 1,
            "active": 1, "accountId": 1, "_id": 0}):
        n_legacy += 1
        sid = r.get("sparkId")
        if sid:
            by_spark.setdefault(str(sid), r)
        ph, nn = norm_phone(r.get("phone")), norm_name(r.get("name"))
        if nn and ph:
            by_namephone.setdefault((nn, ph), r)

    log(f"  roster: {ROSTER_SPARK} {n_spark:,} ({len(by_ref):,} joinable by ref)"
        f"  + {ROSTER} {n_legacy:,}  -> {len(by_spark):,} by sparkId,"
        f" {len(by_namephone):,} by name+phone", verbose=True)
    return by_ref, by_spark, by_namephone


def compose_pass(db, build_id, now, *, verbose=False, dry=False):
    """Second pass: fold the per-source subdocuments into the public shape."""
    t0 = time.time()
    by_ref, by_spark, by_namephone = load_roster(db, verbose=verbose)

    cur = db[AGENTS].find({}, batch_size=200)
    ops, n, conf_counter = [], 0, Counter()

    for doc in cur:
        base, ident, office, brokerage, office_hist = _merge_identity(doc)
        if ident is None:
            continue

        # agentRef is the only key that works for every MLS; try it first.
        roster = by_ref.get(doc["_id"])
        roster_source = ROSTER_SPARK if roster else None
        if roster is None:
            roster = by_spark.get(str(base.get("sparkKey") or "")) or None
            roster_source = "sparkKey" if roster else None
        if roster is None:
            nn, phones = ident.get("nameNormalized"), ident.get("phones") or []
            for p in phones:
                roster = by_namephone.get((nn, p))
                if roster:
                    roster_source = "name+phone"
                    break
        if roster:
            ident.setdefault("licenseNumber", roster.get("licenseNumber"))
            ident["licenseNumber"] = ident.get("licenseNumber") or roster.get("licenseNumber")
            ident["email"] = roster.get("email")
            ident["emailsAll"] = roster.get("emailsAll") or []
            ident["photoUrl"] = roster.get("photoUrl")
            ident["bio"] = roster.get("bio")
            ident["rosterSparkId"] = roster.get("sparkId")
            ident["rosterActive"] = roster.get("active")
            ident["accountId"] = roster.get("accountId")
            ident["rosterSource"] = roster_source
            for src, dst in (("website", "website"), ("officeCity", "rosterOfficeCity"),
                             ("marketingName", "rosterMarketingName")):
                if roster.get(src):
                    ident[dst] = roster[src]
            for src, dst in (("associations", "associations"),
                             ("designations", "designations"),
                             ("languagesSpoken", "languagesSpoken")):
                v = roster.get(src)
                if v:
                    ident[dst] = v if isinstance(v, list) else [v]
            if not ident.get("emailsAll") and roster.get("emails"):
                ident["emailsAll"] = roster["emails"]
            if roster.get("phones"):
                merged = list(dict.fromkeys(
                    (ident.get("phones") or [])
                    + [q for q in (norm_phone(x) for x in roster["phones"]) if q]))
                ident["phones"] = merged[:8]
                ident["phone"] = ident.get("phone") or (merged[0] if merged else None)
            if not ident.get("phone") and norm_phone(roster.get("phone")):
                ident["phone"] = norm_phone(roster.get("phone"))
            if roster.get("isTeam"):
                ident["isTeam"] = True
        else:
            for k in ("licenseNumber", "email", "photoUrl", "bio"):
                ident.setdefault(k, None)
            ident.setdefault("emailsAll", [])

        ident["isSentinel"] = bool(
            is_sentinel_name(ident.get("name"))
            or is_sentinel_name(ident.get("viewName"))
            or (office and is_sentinel_office(office.get("name"))))

        name = ident.get("name") or base.get("agentMlsId") or doc["_id"]
        slug = slugify(f"{name}-{base.get('mlsSource') or ''}-{base.get('agentKey') or ''}")
        ident["slug"] = slug
        ident["searchNames"] = sorted({
            s for s in (
                ident.get("nameNormalized"),
                norm_name(ident.get("viewName")),
                norm_name(ident.get("marketingName")),
                " ".join(reversed((ident.get("nameNormalized") or "").split())),
            ) if s
        })[:6]

        location = _merge_locations(doc)
        activity = _activity(doc, now)
        production = _production(doc)
        coverage = _coverage(doc, base, ident, office, roster, now)
        conf_counter[coverage["confidence"]] += 1

        set_doc = {
            "agentMlsId": base.get("agentMlsId"),
            "agentMlsIdVariants": base.get("agentMlsIdVariants") or [],
            "agentKey": base.get("agentKey"),
            "mlsSource": base.get("mlsSource"),
            "sparkKey": base.get("sparkKey"),
            "slug": slug,
            "identity": ident,
            "location": location,
            "activity": activity,
            "production": production,
            "coverage": coverage,
            "offices": office_hist,
            "lastUpdated": now,
        }
        if office:
            set_doc["office"] = office
        if brokerage:
            set_doc["brokerage"] = brokerage

        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": set_doc}))
        n += 1
        if len(ops) >= BULK and not dry:
            db[AGENTS].bulk_write(ops, ordered=False)
            ops = []
    if ops and not dry:
        db[AGENTS].bulk_write(ops, ordered=False)

    log(f"  composed {n:,} agents   {time.time()-t0:6.1f}s", verbose=True)
    log(f"  confidence: " + "  ".join(f"{k}={v:,}" for k, v in conf_counter.most_common()),
        verbose=True)
    return n


# ── person clustering ─────────────────────────────────────────────────────────

MAX_CLUSTER_MEMBERS = 6


def cluster_people(db, now, *, verbose=False, dry=False):
    """Link the MLS registrations that are the same human.

    Deliberately conservative, because a false merge silently adds someone
    else's sales to an agent's record:

      1. licenseNumber  — a DRE number is one person. Strongest signal, but only
         available where mlsagents matched.
      2. exact normalized name + a shared 10-digit phone.
      3. exact normalized name + the same brokerage brand slug.

    A signature that pulls in more than MAX_CLUSTER_MEMBERS registrations is
    discarded rather than trusted: that is the fingerprint of a common name or a
    feed sentinel, not of one busy agent. Single-member clusters get no personId.
    """
    t0 = time.time()
    rows = list(db[AGENTS].find({}, {
        "identity.nameNormalized": 1, "identity.phones": 1,
        "identity.licenseNumber": 1, "identity.isTeam": 1,
        "brokerage.slug": 1, "mlsSource": 1}))

    parent = {r["_id"]: r["_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    sigs = defaultdict(list)
    for r in rows:
        ident = r.get("identity") or {}
        nn = ident.get("nameNormalized") or ""
        lic = re.sub(r"\W", "", str(ident.get("licenseNumber") or "")).lower()
        if len(lic) >= 6:
            sigs[("lic", lic)].append(r["_id"])
        if nn and len(nn.split()) >= 2:
            for p in ident.get("phones") or []:
                sigs[("np", nn, p)].append(r["_id"])
            bslug = (r.get("brokerage") or {}).get("slug")
            if bslug:
                sigs[("nb", nn, bslug)].append(r["_id"])

    skipped = 0
    for sig, members in sigs.items():
        if len(members) < 2:
            continue
        if len(members) > MAX_CLUSTER_MEMBERS:
            skipped += 1
            continue
        first = members[0]
        for m in members[1:]:
            union(first, m)

    groups = defaultdict(list)
    for aid in parent:
        groups[find(aid)].append(aid)

    meta = {r["_id"]: r for r in rows}
    ops, linked = [], 0
    for root, members in groups.items():
        if len(members) < 2:
            continue
        pid = "p:" + hashlib.sha1(min(members).encode()).hexdigest()[:16]
        for aid in members:
            aka = [{"_id": m,
                    "mlsSource": (meta.get(m) or {}).get("mlsSource")}
                   for m in sorted(members) if m != aid]
            ops.append(UpdateOne({"_id": aid}, {"$set": {
                "identity.personId": pid,
                "identity.alsoKnownAs": aka,
                "identity.registrationCount": len(members),
            }}))
            linked += 1
        if len(ops) >= BULK and not dry:
            db[AGENTS].bulk_write(ops, ordered=False)
            ops = []
    if ops and not dry:
        db[AGENTS].bulk_write(ops, ordered=False)

    if not dry:
        db[AGENTS].update_many(
            {"identity.personId": {"$exists": False}},
            {"$set": {"identity.registrationCount": 1, "identity.alsoKnownAs": []}})

    log(f"  clustered {linked:,} registrations into multi-MLS people "
        f"({skipped:,} ambiguous signatures discarded)   {time.time()-t0:6.1f}s",
        verbose=True)
    return linked


# ── brokerages ────────────────────────────────────────────────────────────────

def build_brokerages(db, build_id, now, *, verbose=False, dry=False):
    """Roll the composed agents up by brokerage brand.

    Grain is the normalized brand slug, not the MLS office id, because one brand
    fragments across many offices — measured on active listings 2026-10-14:
    Compass 153 office ids, Coldwell Banker Realty 101, Century 21 Masters 53.
    Asking "who works at Compass" by office id would return 1/153rd of them.

    Franchise families are NOT collapsed: only legal-suffix and punctuation noise
    is normalized, so "Coldwell Banker Realty" and "Coldwell Banker Home Source"
    remain separate brokerages, which is what they are.
    """
    t0 = time.time()
    pipe = [
        {"$match": {"brokerage.slug": {"$nin": [None, ""]}}},
        {"$group": {
            "_id": "$brokerage.slug",
            "name": {"$max": "$brokerage.name"},
            "normalizedName": {"$first": "$brokerage.normalizedName"},
            "agentCount": {"$sum": 1},
            "activeListings": {"$sum": {"$ifNull": ["$active.total", 0]}},
            "activeVolume": {"$sum": {"$ifNull": ["$active.volume", 0]}},
            "escrowListings": {"$sum": {"$ifNull": ["$escrow.total", 0]}},
            "closedDeals": {"$sum": {"$ifNull": ["$closed.total.deals", 0]}},
            "closedVolume": {"$sum": {"$ifNull": ["$closed.total.volume", 0]}},
            "last12moDeals": {"$sum": {"$ifNull": ["$production.last12moDeals", 0]}},
            "last12moVolume": {"$sum": {"$ifNull": ["$production.last12moVolume", 0]}},
            "liveAgents": {"$sum": {"$cond": ["$activity.hasLiveInventory", 1, 0]}},
            "mlsSources": {"$addToSet": "$mlsSource"},
            "officeRows": {"$push": "$office"},
            "cityRows": {"$push": "$location.cities"},
            "agentRows": {"$push": {
                "_id": "$_id", "name": "$identity.name", "slug": "$slug",
                "city": "$location.primaryCity",
                "isSentinel": {"$ifNull": ["$identity.isSentinel", False]},
                "activeListings": {"$ifNull": ["$active.total", 0]},
                "closedDeals": {"$ifNull": ["$closed.total.deals", 0]},
                "closedVolume": {"$ifNull": ["$closed.total.volume", 0]},
            }},
        }},
    ]

    ops, n = [], 0
    for g in db[AGENTS].aggregate(pipe, allowDiskUse=True, batchSize=50):
        offices = {}
        for o in g.get("officeRows") or []:
            if not o or not o.get("name"):
                continue
            key = (o.get("mlsId") or slugify(o.get("name"))).lower()
            e = offices.setdefault(key, {"mlsId": o.get("mlsId"), "name": o.get("name"),
                                         "phone": o.get("phone"), "agents": 0})
            e["agents"] += 1

        city_totals = defaultdict(lambda: {"agents": 0, "active": 0, "closed": 0})
        for rows in g.get("cityRows") or []:
            for c in rows or []:
                if not c or not c.get("city"):
                    continue
                e = city_totals[c["city"]]
                e["agents"] += 1
                e["active"] += c.get("active", 0)
                e["closed"] += c.get("closed", 0)
        cities = [{"city": c, "slug": slugify(c), **v} for c, v in city_totals.items()]
        cities.sort(key=lambda x: (-x["closed"] - x["active"], x["city"]))

        agents = [a for a in (g.get("agentRows") or [])
                  if a.get("name") and not a.get("isSentinel")]
        top_agents = sorted(agents, key=lambda a: (-(a.get("closedVolume") or 0),
                                                   -(a.get("closedDeals") or 0)))
        office_list = top_n(list(offices.values()), 50, key=lambda x: x["agents"])

        doc = {
            "name": g.get("name"),
            "slug": g["_id"],
            "normalizedName": g.get("normalizedName"),
            "agentCount": g.get("agentCount", 0),
            "agentsWithLiveInventory": g.get("liveAgents", 0),
            "officeCount": len(offices),
            "offices": office_list,
            "mlsSources": sorted(s for s in (g.get("mlsSources") or []) if s),
            "active": {
                "listings": g.get("activeListings", 0),
                "volume": rnd(g.get("activeVolume") or 0),
                "inEscrow": g.get("escrowListings", 0),
            },
            "closed": {
                "deals": g.get("closedDeals", 0),
                "volume": rnd(g.get("closedVolume") or 0),
                "last12moDeals": g.get("last12moDeals", 0),
                "last12moVolume": rnd(g.get("last12moVolume") or 0),
            },
            "cities": cities[:TOP_CITIES],
            "cityCount": len(city_totals),
            "primaryCity": cities[0]["city"] if cities else None,
            "primaryCitySlug": cities[0]["slug"] if cities else None,
            "topAgents": top_agents[:TOP_AGENTS_PER_BROKERAGE],
            "buildId": build_id,
            "lastUpdated": now,
        }
        ops.append(UpdateOne({"_id": g["_id"]}, {"$set": doc}, upsert=True))
        n += 1
        if len(ops) >= 200 and not dry:
            db[BROKERAGES].bulk_write(ops, ordered=False)
            ops = []
    if ops and not dry:
        db[BROKERAGES].bulk_write(ops, ordered=False)

    if not dry:
        stale = db[BROKERAGES].delete_many({"buildId": {"$ne": build_id}}).deleted_count
        if stale:
            log(f"  swept {stale:,} stale brokerages", verbose=True)
    log(f"  built {n:,} brokerages   {time.time()-t0:6.1f}s", verbose=True)
    return n


# ── housekeeping ──────────────────────────────────────────────────────────────

SCRATCH_FIELDS = ["_identity", "_cities", "_zips", "_counties", "_touch",
                  "_unknownOffice"]


def strip_scratch(db, *, dry=False):
    """Remove the per-source staging subdocuments.

    These exist only so the five source passes can hand data to compose_pass.
    Leaving them behind roughly doubles the size of every agent document and
    leaks raw feed values into anything that reads the collection, so this runs
    on every build — including --smoke, which does not sweep.
    """
    if dry:
        return
    db[AGENTS].update_many({}, {"$unset": {f: "" for f in SCRATCH_FIELDS}})
    log("  scratch staging fields removed", verbose=True)


def sweep(db, build_id, *, dry=False, verbose=False):
    """Drop agents that fell out of the window."""
    if dry:
        return 0
    gone = db[AGENTS].delete_many({"buildId": {"$ne": build_id}}).deleted_count
    log(f"  swept {gone:,} agents no longer in the window", verbose=True)
    return gone


INDEXES = [
    # identity / direct lookup
    ([("slug", ASCENDING)], {"name": "slug_1"}),
    ([("agentMlsId", ASCENDING)], {"name": "agentMlsId_1"}),
    ([("agentMlsIdVariants", ASCENDING)], {"name": "agentMlsIdVariants_1"}),
    ([("mlsSource", ASCENDING), ("agentKey", ASCENDING)], {"name": "mlsSource_agentKey"}),
    ([("sparkKey", ASCENDING)], {"name": "sparkKey_1", "sparse": True}),
    ([("identity.personId", ASCENDING)], {"name": "personId_1", "sparse": True}),
    ([("identity.licenseNumber", ASCENDING)], {"name": "license_1", "sparse": True}),
    ([("identity.searchNames", ASCENDING)], {"name": "searchNames_1"}),
    ([("identity.nameNormalized", ASCENDING)], {"name": "nameNormalized_1"}),
    # brokerage
    ([("brokerage.slug", ASCENDING), ("production.windowVolume", DESCENDING)],
     {"name": "brokerage_volume"}),
    ([("office.mlsId", ASCENDING)], {"name": "officeMlsId_1", "sparse": True}),
    # location
    ([("location.primaryCitySlug", ASCENDING), ("production.windowVolume", DESCENDING)],
     {"name": "primaryCity_volume"}),
    ([("location.cities.slug", ASCENDING), ("production.windowVolume", DESCENDING)],
     {"name": "cities_volume"}),
    ([("location.postalCodes.postalCode", ASCENDING)], {"name": "zips_1"}),
    ([("location.counties", ASCENDING)], {"name": "counties_1"}),
    # ranking / filtering
    ([("activity.status", ASCENDING), ("production.windowVolume", DESCENDING)],
     {"name": "status_volume"}),
    ([("production.windowVolume", DESCENDING)], {"name": "windowVolume_-1"}),
    ([("production.windowDeals", DESCENDING)], {"name": "windowDeals_-1"}),
    ([("production.last12moVolume", DESCENDING)], {"name": "l12Volume_-1"}),
    ([("active.total", DESCENDING)], {"name": "activeTotal_-1"}),
    ([("mlsSource", ASCENDING), ("production.windowVolume", DESCENDING)],
     {"name": "mlsSource_volume"}),
    ([("location.primaryCitySlug", ASCENDING), ("brokerage.slug", ASCENDING)],
     {"name": "city_brokerage"}),
]

BROKERAGE_INDEXES = [
    ([("slug", ASCENDING)], {"name": "slug_1"}),
    ([("normalizedName", ASCENDING)], {"name": "normalizedName_1"}),
    ([("closed.volume", DESCENDING)], {"name": "closedVolume_-1"}),
    ([("active.listings", DESCENDING)], {"name": "activeListings_-1"}),
    ([("agentCount", DESCENDING)], {"name": "agentCount_-1"}),
    ([("cities.slug", ASCENDING), ("closed.volume", DESCENDING)], {"name": "cities_volume"}),
    ([("primaryCitySlug", ASCENDING)], {"name": "primaryCitySlug_1"}),
]


# Indexes on the LISTING collections, so "show me this agent's listings" is a
# seek instead of a scan. Without these, get_agent_listings collscans 3.9 GB of
# unified_closed_listings. They are additive and named with an `agentlookup_`
# prefix so they are trivial to identify and drop again.
#
# Keyed on the raw-case listAgentMlsId: agents.agentMlsIdVariants holds every
# casing seen for an agent, so the tool queries `{$in: variants}` and gets an
# index seek without needing a collation or a case-insensitive regex.
LISTING_INDEXES = [
    (SRC_ACTIVE, [("mlsSource", ASCENDING), ("listAgentMlsId", ASCENDING)],
     "agentlookup_listAgent"),
    (SRC_ESCROW, [("mlsSource", ASCENDING), ("listAgentMlsId", ASCENDING)],
     "agentlookup_listAgent"),
    (SRC_OFFMKT, [("mlsSource", ASCENDING), ("listAgentMlsId", ASCENDING)],
     "agentlookup_listAgent"),
    (SRC_CLOSED, [("mlsSource", ASCENDING), ("listAgentMlsId", ASCENDING)],
     "agentlookup_listAgent"),
    (SRC_CLOSED, [("mlsSource", ASCENDING), ("buyerAgentMlsId", ASCENDING)],
     "agentlookup_buyerAgent"),
]


def ensure_listing_indexes(db, *, verbose=False):
    """Create the agent-lookup indexes on the shared listing collections.

    Separate from ensure_indexes() and gated behind its own flag because these
    touch collections this builder does not own — notably a 1.1M-doc, 3.9 GB
    collection that already carries 46 indexes. Built in the background so the
    collections stay readable throughout.
    """
    for coll, keys, name in LISTING_INDEXES:
        try:
            t = time.time()
            db[coll].create_index(keys, name=name, background=True)
            log(f"  {coll:26s} {name:24s} ok  {time.time()-t:5.1f}s", verbose=True)
        except OperationFailure as e:
            log(f"  ! {coll} {name}: {str(e)[:120]}", verbose=True)


def ensure_indexes(db, *, verbose=False):
    made = 0
    for keys, opts in INDEXES:
        try:
            db[AGENTS].create_index(keys, background=True, **opts)
            made += 1
        except OperationFailure as e:
            log(f"  ! agents index {opts.get('name')}: {str(e)[:110]}", verbose=True)
    for keys, opts in BROKERAGE_INDEXES:
        try:
            db[BROKERAGES].create_index(keys, background=True, **opts)
            made += 1
        except OperationFailure as e:
            log(f"  ! brokerages index {opts.get('name')}: {str(e)[:110]}", verbose=True)
    # Free-text search over names + brokerage, for the agent-search tool.
    try:
        db[AGENTS].create_index(
            [("identity.name", "text"), ("brokerage.name", "text"),
             ("office.name", "text")],
            name="agent_text", background=True,
            weights={"identity.name": 10, "brokerage.name": 3, "office.name": 2})
        made += 1
    except OperationFailure as e:
        log(f"  ! text index: {str(e)[:110]}", verbose=True)
    log(f"  ensured {made} indexes", verbose=True)
    return made


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build the agents + brokerages database")
    ap.add_argument("--all", action="store_true", help="full rebuild (default)")
    ap.add_argument("--years", type=int, default=5,
                    help="closed-deal window in years (default 5, matches the TTL)")
    ap.add_argument("--smoke", action="store_true",
                    help="cap every source scan for a fast correctness check")
    ap.add_argument("--smoke-limit", type=int, default=40000)
    ap.add_argument("--skip-closed", action="store_true",
                    help="skip the 1.1M-doc closed scan (identity/active only)")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--no-sweep", action="store_true",
                    help="keep agents from older builds")
    ap.add_argument("--indexes-only", action="store_true")
    ap.add_argument("--listing-indexes", action="store_true",
                    help="also index listAgentMlsId / buyerAgentMlsId on the "
                         "listing collections (needed by get_agent_listings)")
    ap.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    client = MongoClient(MONGO_URI)
    db = client.get_database()
    now = datetime.now(timezone.utc)
    build_id = now.strftime("%Y%m%dT%H%M%SZ")
    cutoff = now - timedelta(days=365 * args.years)
    ceiling = now + timedelta(days=7)   # drops the 35 corrupt future closeDates
    limit = args.smoke_limit if args.smoke else None

    print(f"\n{'='*74}")
    print(f"  build-agents  db={db.name}  build={build_id}")
    print(f"  window: {cutoff.date()} .. {now.date()}  ({args.years}y)"
          f"{'   [SMOKE cap=' + str(limit) + ']' if limit else ''}"
          f"{'   [DRY RUN]' if args.dry_run else ''}")
    print(f"{'='*74}\n")

    # compose_pass reads the per-source staging subdocuments, and strip_scratch
    # removes those at the end of every build. So skipping BOTH source stages
    # leaves it nothing to compose and it would quietly rewrite nothing at all
    # — including after a fresh roster sync, which is exactly when someone
    # would reach for these flags. Refuse rather than no-op.
    if args.skip_live and args.skip_closed and not args.indexes_only:
        raise SystemExit(
            "--skip-live together with --skip-closed leaves compose_pass no "
            "staging data to read (the previous build stripped it), so nothing "
            "would be written. Run a full --all instead; to pick up a new "
            "agent_roster, a full rebuild is what re-joins contact detail.")

    if args.indexes_only:
        ensure_indexes(db, verbose=args.verbose)
        if args.listing_indexes:
            print("\nlisting-collection agent indexes:")
            ensure_listing_indexes(db, verbose=args.verbose)
        return

    t0 = time.time()

    if not args.skip_live:
        print("live sources (list side):")
        run_live_pass(db, SRC_ACTIVE, "active", build_id,
                      limit=limit, verbose=args.verbose, dry=args.dry_run)
        run_live_pass(db, SRC_ESCROW, "escrow", build_id,
                      limit=limit, verbose=args.verbose, dry=args.dry_run)
        run_live_pass(db, SRC_OFFMKT, "offMarket", build_id,
                      limit=limit, verbose=args.verbose, dry=args.dry_run)

    if not args.skip_closed:
        print("\nclosed sales + leases (both sides, one scan):")
        run_closed_pass(db, build_id, cutoff, ceiling, args.years, now,
                        limit=limit, verbose=args.verbose, dry=args.dry_run)

    if not args.dry_run:
        print("\ncompose:")
        compose_pass(db, build_id, now, verbose=args.verbose, dry=args.dry_run)
        cluster_people(db, now, verbose=args.verbose, dry=args.dry_run)

        print("\nbrokerages:")
        build_brokerages(db, build_id, now, verbose=args.verbose, dry=args.dry_run)

        print("\nhousekeeping:")
        if not args.no_sweep and not args.smoke:
            sweep(db, build_id, dry=args.dry_run, verbose=args.verbose)
        else:
            log("  stale sweep skipped", verbose=True)
        strip_scratch(db, dry=args.dry_run)
        ensure_indexes(db, verbose=args.verbose)
        if args.listing_indexes:
            print("\nlisting-collection agent indexes:")
            ensure_listing_indexes(db, verbose=args.verbose)

        print("\nresult:")
        print(f"  agents      {db[AGENTS].count_documents({}):>9,}")
        print(f"  brokerages  {db[BROKERAGES].count_documents({}):>9,}")
        sent = db[AGENTS].count_documents({"identity.isSentinel": True})
        print(f"  of which MLS placeholder (not real agents): {sent:,}")

    print(f"\ndone in {(time.time()-t0)/60:.1f} min\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
