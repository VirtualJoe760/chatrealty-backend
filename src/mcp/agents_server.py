#!/usr/bin/env python3
"""
MCP server over the agent + brokerage database.

Serves the `agents` and `brokerages` collections built by
src/scripts/agents/build-agents.py. Every tool here is a lookup or an indexed
find — nothing is computed at call time, per
docs/strategies/tool-development.md. If a figure is not in the database, the
tool says so rather than deriving it.

Transport is stdio by default, which is what Claude Code on this VPS uses. The
same server runs over HTTP for a hosted deployment:

    python3 src/mcp/agents_server.py --transport streamable-http --port 8787

Design rules each tool follows (from the strategy doc):
  * tight projections — a full agent doc is large; list tools return a card
  * every row carries `agent` (the _id) so the model can chain without guessing
  * deterministic sort, always, so repeated calls agree
  * results are capped, and the response states the cap and the true total
  * the absent case is explicit and explains itself
  * MLS placeholder records (identity.isSentinel) are excluded from rankings by
    default, because they out-produce every real agent and would top every list
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from pymongo import DESCENDING, MongoClient

REPO = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=REPO / ".env.local")

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise SystemExit("Missing MONGODB_URI in .env.local")

_client = MongoClient(MONGO_URI, tz_aware=True)
_db = _client.get_database()
AGENTS = _db["agents"]
BROKERAGES = _db["brokerages"]

LISTING_COLLECTIONS = {
    "active": ("unified_listings", "listAgentMlsId", "listPrice"),
    "escrow": ("unified_in_escrow", "listAgentMlsId", "listPrice"),
    "offmarket": ("unified_off_market", "listAgentMlsId", "listPrice"),
    "closed": ("unified_closed_listings", "listAgentMlsId", "closePrice"),
    "closed_buyside": ("unified_closed_listings", "buyerAgentMlsId", "closePrice"),
}

MAX_LIMIT = 100

# A list row. Deliberately small: ~15 fields instead of the ~120 on the doc.
CARD = {
    "slug": 1, "agentMlsId": 1, "mlsSource": 1,
    "identity.name": 1, "identity.isTeam": 1, "identity.isSentinel": 1,
    "identity.phone": 1, "identity.personId": 1,
    "brokerage.name": 1, "brokerage.slug": 1,
    "office.name": 1,
    "location.primaryCity": 1, "location.marketLabel": 1,
    "production.windowDeals": 1, "production.windowVolume": 1,
    "production.last12moDeals": 1, "production.last12moVolume": 1,
    "production.activeListings": 1, "production.primaryRole": 1,
    "activity.status": 1, "coverage.confidence": 1,
}

LISTING_FIELDS = {
    "listingKey": 1, "listingId": 1, "address": 1, "city": 1, "postalCode": 1,
    "subdivisionName": 1, "listPrice": 1, "closePrice": 1, "closeDate": 1,
    "onMarketDate": 1, "standardStatus": 1, "propertyType": 1,
    "propertySubType": 1, "bedroomsTotal": 1, "bedsTotal": 1,
    "bathroomsTotalInteger": 1, "livingArea": 1, "latitude": 1, "longitude": 1,
    "slug": 1, "slugAddress": 1, "_id": 0,
}

SORTS = {
    "volume": "production.windowVolume",
    "deals": "production.windowDeals",
    "last12mo_volume": "production.last12moVolume",
    "last12mo_deals": "production.last12moDeals",
    "active_listings": "production.activeListings",
}

server = MCPServer(
    name="jpsrealtor-agents",
    version="1.0.0",
    instructions=(
        "Agent and brokerage records derived from the MLS feeds. Figures are "
        "precomputed by a cron job; report them as given and cite "
        "`lastUpdated`. Two things to respect:\n"
        "1. `identity.isSentinel` marks MLS placeholder ids (out-of-area / "
        "non-member co-op buckets). They are not people. They are excluded "
        "from rankings by default — never present one as a top agent.\n"
        "2. One person can hold registrations in several MLSs. "
        "`identity.personId` and `identity.alsoKnownAs` link them; a single "
        "record's production is that registration only, not the person's "
        "whole career. Say so when it matters.\n"
        "Treat production as listing-side plus buyer-side credit within the "
        "stated window, and present other agents' numbers neutrally."
    ),
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _clamp(n: int | None, default: int = 20) -> int:
    if not n or n < 1:
        return default
    return min(int(n), MAX_LIMIT)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def _norm(s: str) -> str:
    s = re.sub(r"[.,'\"()]", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _freshness() -> dict:
    d = AGENTS.find_one({}, {"lastUpdated": 1, "closed.windowYears": 1,
                             "closed.windowStart": 1})
    if not d:
        return {"built": False}
    return {
        "built": True,
        "lastUpdated": d.get("lastUpdated"),
        "closedWindowYears": (d.get("closed") or {}).get("windowYears"),
        "closedWindowStart": (d.get("closed") or {}).get("windowStart"),
    }


def _not_built() -> dict:
    return {
        "found": False,
        "error": "The agents collection is empty or missing.",
        "remedy": ("Run `python3 src/scripts/agents/build-agents.py --all` on "
                   "the VPS. The MCP server cannot trigger the build; it only "
                   "reads what the cron job wrote."),
    }


def _capped(rows: list, total: int, limit: int) -> dict:
    out = {"count": len(rows), "matched": total}
    if total > len(rows):
        out["truncated"] = True
        out["note"] = (f"Showing {len(rows)} of {total:,} matches, ranked by the "
                       f"requested sort. Raise `limit` (max {MAX_LIMIT}) or "
                       f"narrow the filters to see others.")
    return out


def _resolve_agent(ref: str) -> tuple[dict | None, list[dict]]:
    """Accept an _id, a slug, a bare MLS id, or a name. Never guess.

    Returns (exact_doc, candidates). When exact_doc is None and candidates is
    non-empty the caller must disambiguate instead of picking one.
    """
    ref = (ref or "").strip()
    if not ref:
        return None, []

    for q in ({"_id": ref}, {"slug": ref}, {"slug": _slug(ref)}):
        d = AGENTS.find_one(q)
        if d:
            return d, []

    # a bare mls id, any casing
    hits = list(AGENTS.find({"agentMlsIdVariants": ref}, CARD).limit(10))
    if not hits:
        hits = list(AGENTS.find({"agentMlsId": re.compile(f"^{re.escape(ref)}$", re.I)},
                                CARD).limit(10))
    if len(hits) == 1:
        return AGENTS.find_one({"_id": hits[0]["_id"]}), []
    if hits:
        return None, hits

    # a name
    n = _norm(ref)
    hits = list(AGENTS.find({"identity.searchNames": n}, CARD)
                .sort("production.windowVolume", DESCENDING).limit(10))
    if not hits:
        hits = list(AGENTS.find(
            {"identity.nameNormalized": re.compile(re.escape(n), re.I)}, CARD)
            .sort("production.windowVolume", DESCENDING).limit(10))
    if len(hits) == 1:
        return AGENTS.find_one({"_id": hits[0]["_id"]}), []
    return None, hits


def _ambiguous(ref: str, cands: list[dict]) -> dict:
    return {
        "found": False,
        "reason": "ambiguous",
        "query": ref,
        "message": (f"{len(cands)} agent records match. Pass one of these "
                    f"`agent` values exactly. Records in different MLSs can be "
                    f"the same person — check `identity.personId`."),
        "candidates": cands,
    }


def _attach_local(rows: list[dict], city: str | None) -> list[dict]:
    """Surface the agent's precomputed numbers *for the filtered city*.

    A city filter matches anyone who listed or closed there, and the sort is on
    career-wide production, so a 1,609-deal agent based two counties away can
    outrank everyone actually working the city. Nothing is computed here — the
    per-city row already exists on the document as location.cities[] — but
    showing it lets the model say "44 of their 1,609 deals were in Palm Desert"
    instead of implying they are a local leader.
    """
    if not city:
        return rows
    want = _slug(city)
    ids = [r["_id"] for r in rows]
    local = {}
    for d in AGENTS.find({"_id": {"$in": ids}}, {"location.cities": 1}):
        for c in ((d.get("location") or {}).get("cities") or []):
            if c.get("slug") == want:
                local[d["_id"]] = {
                    "city": c.get("city"), "closed": c.get("closed", 0),
                    "active": c.get("active", 0), "escrow": c.get("escrow", 0),
                    "total": c.get("total", 0),
                }
                break
    for r in rows:
        r["inThisCity"] = local.get(r["_id"])
    return rows


def _min_city_deals_filter(city, postal_code, min_city_deals):
    """Require N closed deals in the filtered city, not just presence."""
    if not min_city_deals:
        return None
    n = int(min_city_deals)
    if city:
        return {"location.cities": {"$elemMatch": {"slug": _slug(city),
                                                   "closed": {"$gte": n}}}}
    if postal_code:
        return {"location.postalCodes": {"$elemMatch": {
            "postalCode": str(postal_code).strip()[:5], "count": {"$gte": n}}}}
    return None


def _location_filter(city=None, postal_code=None, county=None) -> dict:
    """City matches anywhere the agent does business, not just their primary."""
    q: dict[str, Any] = {}
    if city:
        q["location.cities.slug"] = _slug(city)
    if postal_code:
        q["location.postalCodes.postalCode"] = str(postal_code).strip()[:5]
    if county:
        q["location.counties"] = re.compile(f"^{re.escape(county.strip())}$", re.I)
    return q


# ── tools ─────────────────────────────────────────────────────────────────────

@server.tool(
    description=(
        "Find agents by name, and/or filter by city, brokerage, MLS, or "
        "activity. Use this when the user names an agent, or asks 'who are the "
        "agents in X'. Returns compact cards; call get_agent for everything "
        "known about one."),
)
def search_agents(
    name: str | None = None,
    city: str | None = None,
    postal_code: str | None = None,
    county: str | None = None,
    brokerage: str | None = None,
    mls_source: str | None = None,
    status: Literal["active", "recent", "dormant", "any"] = "any",
    has_active_listings: bool | None = None,
    min_closed_deals: int | None = None,
    min_city_deals: int | None = None,
    include_placeholders: bool = False,
    sort_by: Literal["volume", "deals", "last12mo_volume", "last12mo_deals",
                     "active_listings"] = "volume",
    limit: int | None = 20,
) -> dict:
    if AGENTS.estimated_document_count() == 0:
        return _not_built()

    limit = _clamp(limit)
    q: dict[str, Any] = {}
    q.update(_location_filter(city, postal_code, county))

    if name:
        n = _norm(name)
        q["$or"] = [
            {"identity.searchNames": n},
            {"identity.nameNormalized": re.compile(re.escape(n), re.I)},
        ]
    if brokerage:
        bslug = _slug(brokerage)
        q["$and"] = q.get("$and", []) + [{"$or": [
            {"brokerage.slug": bslug},
            {"brokerage.name": re.compile(re.escape(brokerage.strip()), re.I)},
        ]}]
    if mls_source:
        q["mlsSource"] = mls_source.strip().upper()
    if status != "any":
        q["activity.status"] = status
    if has_active_listings is not None:
        q["activity.hasLiveInventory"] = bool(has_active_listings)
    if min_closed_deals:
        q["production.windowDeals"] = {"$gte": int(min_closed_deals)}
    mcd = _min_city_deals_filter(city, postal_code, min_city_deals)
    if mcd:
        q.update(mcd)
    if not include_placeholders:
        q["identity.isSentinel"] = {"$ne": True}

    sort_field = SORTS.get(sort_by, SORTS["volume"])
    total = AGENTS.count_documents(q)
    rows = _attach_local(
        list(AGENTS.find(q, CARD).sort([(sort_field, DESCENDING), ("_id", 1)])
             .limit(limit)), city)

    out = {"found": bool(rows), "agents": rows, "sortedBy": sort_by,
           "filters": {k: v for k, v in {
               "name": name, "city": city, "postalCode": postal_code,
               "county": county, "brokerage": brokerage,
               "mlsSource": mls_source, "status": status,
               "minClosedDeals": min_closed_deals,
               "minCityDeals": min_city_deals,
               "includePlaceholders": include_placeholders}.items() if v},
           **_capped(rows, total, limit), **_freshness()}
    if not rows:
        out["note"] = ("No agent matched. Names come from the MLS feed and may "
                       "be spelled differently there (middle initials, "
                       "nicknames, team names). Try a surname alone, or drop a "
                       "filter. Note that agents are stored per MLS "
                       "registration, so a city filter only matches where they "
                       "have actually listed or closed.")
    return out


@server.tool(
    description=(
        "Everything the database holds on one agent: identity and licence, "
        "brokerage and office history, the cities and ZIPs they work, active "
        "and in-escrow inventory, closed production by side and by year, and "
        "the data-quality caveats. Accepts an _id, slug, MLS id or name."),
)
def get_agent(agent: str, include_listings: bool = False,
              listings_limit: int | None = 10) -> dict:
    if AGENTS.estimated_document_count() == 0:
        return _not_built()

    doc, cands = _resolve_agent(agent)
    if doc is None:
        if cands:
            return _ambiguous(agent, cands)
        return {"found": False, "query": agent,
                "message": ("No agent record with that id, slug, MLS id or "
                            "name. Try search_agents with a partial name.")}

    doc.pop("buildId", None)
    out = {"found": True, "agent": doc}

    ident = doc.get("identity") or {}
    if ident.get("isSentinel"):
        out["warning"] = (
            "This is an MLS placeholder record, not a person. Its production "
            "is the sum of many unrelated agents' co-op deals. Do not present "
            "it as an agent.")
    if ident.get("alsoKnownAs"):
        out["samePersonElsewhere"] = {
            "personId": ident.get("personId"),
            "registrations": ident["alsoKnownAs"],
            "note": ("The figures above cover this MLS registration only. "
                     "Call get_agent on the others and add them for a career "
                     "total, but watch for a deal that appears in both."),
        }
    if include_listings:
        out["listings"] = {
            kind: get_agent_listings(agent=doc["_id"], kind=kind,
                                     limit=_clamp(listings_limit, 10))
            for kind in ("active", "closed")
        }
    return out


@server.tool(
    description=(
        "The actual listings behind an agent's numbers: 'active' (on market "
        "now), 'escrow', 'closed' (where they were the listing agent), "
        "'closed_buyside' (where they represented the buyer), or 'offmarket'. "
        "Use after get_agent when the user wants the properties, not the "
        "totals."),
)
def get_agent_listings(
    agent: str,
    kind: Literal["active", "escrow", "closed", "closed_buyside",
                  "offmarket"] = "active",
    limit: int | None = 25,
) -> dict:
    if AGENTS.estimated_document_count() == 0:
        return _not_built()

    doc, cands = _resolve_agent(agent)
    if doc is None:
        return _ambiguous(agent, cands) if cands else {
            "found": False, "query": agent, "message": "No such agent record."}

    coll_name, field, price_field = LISTING_COLLECTIONS[kind]
    variants = doc.get("agentMlsIdVariants") or (
        [doc["agentMlsId"]] if doc.get("agentMlsId") else [])
    if not variants:
        return {"found": False, "agent": doc["_id"],
                "message": "This record has no MLS id to look listings up by."}

    limit = _clamp(limit, 25)
    q = {"mlsSource": doc.get("mlsSource"), field: {"$in": variants}}
    sort = ([("closeDate", DESCENDING)] if "closed" in kind
            else [("onMarketDate", DESCENDING)])

    total = _db[coll_name].count_documents(q)
    rows = list(_db[coll_name].find(q, LISTING_FIELDS).sort(sort).limit(limit))

    return {
        "found": bool(rows),
        "agent": doc["_id"],
        "agentName": (doc.get("identity") or {}).get("name"),
        "kind": kind,
        "source": coll_name,
        "role": "buyer's agent" if kind == "closed_buyside" else "listing agent",
        "listings": rows,
        "priceField": price_field,
        **_capped(rows, total, limit),
        "note": ("Closed listings are capped at the TTL window (5 years) by the "
                 "database itself, so older deals are gone, not missing."
                 if "closed" in kind else None),
    }


@server.tool(
    description=(
        "Rank agents by production in a market or at a brokerage — 'top "
        "agents in Palm Desert', 'biggest producers at Compass'. MLS "
        "placeholder ids are excluded. Returns precomputed figures; do not "
        "recompute or re-rank them. The sort is on TOTAL production, so with a "
        "city filter also read `inThisCity` on each row, and pass "
        "`min_city_deals` (e.g. 5) to exclude agents who merely passed "
        "through."),
)
def rank_agents(
    city: str | None = None,
    postal_code: str | None = None,
    county: str | None = None,
    brokerage: str | None = None,
    mls_source: str | None = None,
    by: Literal["volume", "deals", "last12mo_volume", "last12mo_deals",
                "active_listings"] = "volume",
    min_city_deals: int | None = None,
    only_with_active_listings: bool = False,
    limit: int | None = 10,
) -> dict:
    if AGENTS.estimated_document_count() == 0:
        return _not_built()

    limit = _clamp(limit, 10)
    q: dict[str, Any] = {"identity.isSentinel": {"$ne": True}}
    q.update(_location_filter(city, postal_code, county))
    if brokerage:
        q["brokerage.slug"] = _slug(brokerage)
    if mls_source:
        q["mlsSource"] = mls_source.strip().upper()
    if only_with_active_listings:
        q["activity.hasLiveInventory"] = True
    mcd = _min_city_deals_filter(city, postal_code, min_city_deals)
    if mcd:
        q.update(mcd)

    field = SORTS.get(by, SORTS["volume"])
    q[field] = {"$gt": 0}

    total = AGENTS.count_documents(q)
    rows = _attach_local(
        list(AGENTS.find(q, CARD).sort([(field, DESCENDING), ("_id", 1)])
             .limit(limit)), city)

    out = {
        "found": bool(rows), "rankedBy": by, "metricField": field,
        "scope": {k: v for k, v in {
            "city": city, "postalCode": postal_code, "county": county,
            "brokerage": brokerage, "mlsSource": mls_source}.items() if v} or "statewide",
        "agents": rows,
        **_capped(rows, total, limit), **_freshness(),
        "caveats": [
            "Production is listing-side plus buyer-side credit inside the "
            "closed window; an agent who mostly represents buyers ranks on "
            "that work too (see production.primaryRole).",
            "IMPORTANT when a city is given: the ranking metric is each "
            "agent's TOTAL production, not their production in that city, so a "
            "high-volume agent from elsewhere who closed once here can outrank "
            "the local leaders. `inThisCity` on every row gives their "
            "precomputed local numbers — quote those, or pass "
            "`min_city_deals` to require real local activity.",
            "Records are per MLS registration. Someone active in two MLSs is "
            "split across two rows — identity.personId links them.",
            "MLS placeholder ids (out-of-area / non-member buckets) are "
            "excluded; they would otherwise rank first.",
        ],
    }
    if not rows:
        out["note"] = ("Nothing matched. Thin markets genuinely have no ranked "
                       "agents for a metric; try `by='deals'` or widen to the "
                       "county.")
    return out


@server.tool(
    description=(
        "Brokerages, ranked or filtered by city — 'biggest brokerages in La "
        "Quinta', 'how many agents does Compass have'. Grain is the brokerage "
        "brand, which spans many MLS offices."),
)
def list_brokerages(
    city: str | None = None,
    name: str | None = None,
    by: Literal["closed_volume", "closed_deals", "active_listings",
                "agents"] = "closed_volume",
    limit: int | None = 15,
) -> dict:
    if BROKERAGES.estimated_document_count() == 0:
        return _not_built()

    limit = _clamp(limit, 15)
    q: dict[str, Any] = {}
    if city:
        q["cities.slug"] = _slug(city)
    if name:
        q["$or"] = [{"slug": _slug(name)},
                    {"name": re.compile(re.escape(name.strip()), re.I)}]

    field = {"closed_volume": "closed.volume", "closed_deals": "closed.deals",
             "active_listings": "active.listings", "agents": "agentCount"}[by]
    proj = {"name": 1, "slug": 1, "agentCount": 1, "agentsWithLiveInventory": 1,
            "officeCount": 1, "active": 1, "closed": 1, "primaryCity": 1,
            "mlsSources": 1, "cityCount": 1}

    total = BROKERAGES.count_documents(q)
    rows = list(BROKERAGES.find(q, proj)
                .sort([(field, DESCENDING), ("_id", 1)]).limit(limit))
    return {
        "found": bool(rows), "rankedBy": by, "brokerages": rows,
        "scope": {"city": city} if city else "statewide",
        **_capped(rows, total, limit), **_freshness(),
        "note": ("Counts cover agents who listed or closed in the window, not "
                 "a brokerage's official roster. 31% of active listings report "
                 "'Unknown Office', and those agents have no brokerage here at "
                 "all, so these totals are a floor."),
    }


@server.tool(
    description=(
        "One brokerage in full: office list, city footprint, agent count, "
        "active and closed production, and its top agents."),
)
def get_brokerage(brokerage: str, top_agents: int | None = 25) -> dict:
    if BROKERAGES.estimated_document_count() == 0:
        return _not_built()

    ref = (brokerage or "").strip()
    doc = BROKERAGES.find_one({"slug": ref}) or BROKERAGES.find_one({"slug": _slug(ref)})
    if not doc:
        cands = list(BROKERAGES.find(
            {"name": re.compile(re.escape(ref), re.I)},
            {"name": 1, "slug": 1, "agentCount": 1, "primaryCity": 1})
            .sort("closed.volume", DESCENDING).limit(10))
        if len(cands) == 1:
            doc = BROKERAGES.find_one({"_id": cands[0]["_id"]})
        elif cands:
            return {"found": False, "reason": "ambiguous", "query": ref,
                    "message": ("Several brokerages match. Pass one `slug` "
                                "exactly. Franchise brands are kept separate "
                                "on purpose — 'Coldwell Banker Realty' and "
                                "'Coldwell Banker Home Source' are different "
                                "brokerages."),
                    "candidates": cands}
        else:
            return {"found": False, "query": ref,
                    "message": "No brokerage by that name or slug."}

    doc.pop("buildId", None)
    n = _clamp(top_agents, 25)
    doc["topAgents"] = (doc.get("topAgents") or [])[:n]
    return {"found": True, "brokerage": doc, **_freshness()}


@server.tool(
    description=(
        "What this database covers and how current it is: row counts, the "
        "closed window, confidence spread, and the known data-quality limits. "
        "Call this when the user asks how complete or how fresh the agent data "
        "is, or before making a claim about coverage."),
)
def agent_database_stats() -> dict:
    n = AGENTS.estimated_document_count()
    if n == 0:
        return _not_built()

    def c(q):
        return AGENTS.count_documents(q)

    by_mls = {r["_id"]: r["n"] for r in AGENTS.aggregate(
        [{"$group": {"_id": "$mlsSource", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}])}
    by_conf = {r["_id"]: r["n"] for r in AGENTS.aggregate(
        [{"$group": {"_id": "$coverage.confidence", "n": {"$sum": 1}}}])}
    by_status = {r["_id"]: r["n"] for r in AGENTS.aggregate(
        [{"$group": {"_id": "$activity.status", "n": {"$sum": 1}}}])}

    return {
        "agents": n,
        "brokerages": BROKERAGES.estimated_document_count(),
        "byMlsSource": by_mls,
        "byConfidence": by_conf,
        "byActivityStatus": by_status,
        "withActiveListings": c({"activity.hasLiveInventory": True}),
        "withClosedDeals": c({"production.windowDeals": {"$gt": 0}}),
        "withBrokerage": c({"brokerage.slug": {"$exists": True}}),
        "withLicenceNumber": c({"identity.licenseNumber": {"$nin": [None, ""]}}),
        "withEmail": c({"identity.email": {"$nin": [None, ""]}}),
        "multiMlsPeople": c({"identity.personId": {"$exists": True}}),
        "mlsPlaceholderRecords": c({"identity.isSentinel": True}),
        **_freshness(),
        "limits": [
            "One document per MLS registration, not per person. A person "
            "licensed in two MLSs has two records; identity.personId links "
            "them where a licence number or an exact name plus a shared phone "
            "or brokerage proved it.",
            "Brokerage is unknown for the agents whose listings all report the "
            "'Unknown Office' sentinel — 31% of active listings feed-wide.",
            "Buyer-side records carry a display name only: the feed has no "
            "first/last name, phone or office id on that side.",
            "Closed production is limited to the window, and the database "
            "itself TTL-deletes closed listings after 5 years, so older "
            "career history cannot be recovered from this data.",
            "Email, licence number and photo come from the mlsagents roster, "
            "which covers the desert MLS only (4,720 agents).",
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="MCP server for the agent database")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "sse", "streamable-http"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.settings.host = args.host
        server.settings.port = args.port
        server.run(args.transport)


if __name__ == "__main__":
    main()
