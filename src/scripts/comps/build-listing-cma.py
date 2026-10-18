#!/usr/bin/env python3
"""
Build Listing CMA

Nightly per-listing aggregator: for every active listing in `unified_listings`,
runs the same 5-level hierarchy / 12-factor scoring engine that the frontend
runs on-demand, and persists the result as `cmaStats` on the listing doc. The
frontend then becomes a `findOne` lookup instead of a 1–20s API round-trip.

Match rule (Level 1): case-insensitive exact match of
`unified_listings.subdivisionName` against the subject's subdivisionName.
Levels 2–5 fall back to relaxed sqft, then city, then geographic radius.

Usage:
    # Single listing (smoke test)
    python3 src/scripts/cma/build-listing-cma.py --key 20260306... --dry-run

    # Single city
    python3 src/scripts/cma/build-listing-cma.py --city "Indian Wells"

    # Cap to first N for testing
    python3 src/scripts/cma/build-listing-cma.py --all --limit 100 --dry-run

    # Twice-weekly cron (default: skip listings whose cmaStats is < 60h old)
    python3 src/scripts/cma/build-listing-cma.py --all

    # Force a full rebuild ignoring staleness
    python3 src/scripts/cma/build-listing-cma.py --all --skip-fresh-hours 0

Suggested cron (twice a week at 1 AM, Mon + Thu):
    0 1 * * 1,4 cd /root/jpsrealtor && .venv/bin/python3 src/scripts/cma/build-listing-cma.py --all >> /var/log/listing-cma.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

# ──────────────────────────────────────────────────────────────────────────────
# 📐 CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

RESIDENTIAL_PROPERTY_TYPE = "A"
ACTIVE_STATUSES = ["Active", "ComingSoon"]
TOP_COMPS = 5
LEVEL_CANDIDATE_LIMIT = 20      # per spec: cap candidates per level before scoring
MIN_COMPS_PER_STATUS = 3        # stop the level cascade once this is hit
BULK_BATCH = 50

# subdivisionName values that are placeholders, not real subdivisions. When
# the subject's name is one of these, we cannot use L1/L2 — those would pull
# any "Not Applicable" listing from any city, which is exactly the bug fixed
# 2026-05-05. Mirrors EXCLUDED_SUBDIVISIONS in build_search_index.py.
PLACEHOLDER_SUBDIVISIONS = {
    "not applicable", "n/a", "none", "other", "na", "no hoa",
    "see remarks", "unknown", "tbd", "various", "not in a development",
    "",
}


def _is_real_subdivision(name) -> bool:
    if not isinstance(name, str):
        return False
    return name.strip().lower() not in PLACEHOLDER_SUBDIVISIONS

# Projection — only fetch fields actually consumed by scoring + shaping. Each
# candidate doc is otherwise ~5KB (full publicRemarks etc.) and we may pull 200
# per subject across 5 levels × 2 collections. Cutting this is the single
# biggest throughput win.
COMP_PROJECTION = {
    "listingKey": 1, "listingId": 1, "city": 1, "subdivisionName": 1,
    "propertyType": 1, "propertySubType": 1,
    "listPrice": 1, "originalListPrice": 1, "currentPrice": 1,
    "closePrice": 1, "closeDate": 1,
    "livingArea": 1, "lotSizeSquareFeet": 1, "lotSizeArea": 1,
    "bedsTotal": 1, "bedroomsTotal": 1,
    "bathsTotal": 1, "bathroomsTotalDecimal": 1, "bathroomsTotalInteger": 1,
    "yearBuilt": 1, "garageSpaces": 1,
    "poolYn": 1, "pool": 1, "spaYn": 1, "spa": 1,
    "view": 1, "viewYn": 1,
    "gatedCommunityYn": 1, "seniorCommunityYn": 1,
    "architecturalStyle": 1, "levels": 1, "stories": 1,
    "landType": 1,
    "daysOnMarket": 1, "onMarketDate": 1, "listingContractDate": 1,
    "coordinates": 1, "latitude": 1, "longitude": 1,
    "streetNumber": 1, "streetName": 1, "streetSuffix": 1,
    "unparsedAddress": 1, "address": 1,
    "_id": 1,
}

# Tier search parameters (port of TIER_PARAMS from engine.ts)
TIER_PARAMS = {
    "affordable": {
        "sqftRange": 300,
        "radiusFallbackMiles": 3,
        "yearBuiltTolerance": 15,
        "maxCompsPerStatus": 5,
        "lookbackMonths": {1: 6,  2: 12, 3: 6,  4: 24, 5: 36},
    },
    "residential": {
        "sqftRange": 400,
        "radiusFallbackMiles": 2,
        "yearBuiltTolerance": 10,
        "maxCompsPerStatus": 5,
        "lookbackMonths": {1: 6,  2: 12, 3: 6,  4: 24, 5: 36},
    },
    "luxury": {
        "sqftRange": 600,
        "radiusFallbackMiles": 5,
        "yearBuiltTolerance": 10,
        "maxCompsPerStatus": 5,
        "lookbackMonths": {1: 9,  2: 18, 3: 9,  4: 30, 5: 48},
    },
}

# 12-factor scoring weights by tier
SCORING_WEIGHTS = {
    "affordable":  {"sqft": 25, "subdivision": 12, "bedBath": 17, "poolSpa": 5,  "lotSize": 5,  "view": 3,  "recency": 13, "yearBuilt": 8, "archStyle": 0, "stories": 5, "garage": 4, "landType": 3},
    "residential": {"sqft": 20, "subdivision": 15, "bedBath": 12, "poolSpa": 10, "lotSize": 10, "view": 8,  "recency": 10, "yearBuilt": 5, "archStyle": 3, "stories": 3, "garage": 2, "landType": 2},
    "luxury":      {"sqft": 17, "subdivision": 20, "bedBath": 8,  "poolSpa": 10, "lotSize": 15, "view": 13, "recency": 5,  "yearBuilt": 2, "archStyle": 6, "stories": 2, "garage": 1, "landType": 1},
}

# View categories — used by both detection and scoring
VIEW_CATEGORIES = {
    "golf":     ["golf course", "golf view", "on the golf", "fairway", "green view"],
    "mountain": ["mountain view", "mountain vista", "san jacinto", "santa rosa"],
    "desert":   ["desert view", "desert vista", "valley view"],
    "water":    ["ocean view", "lake view", "water view", "harbor view", "bay view"],
    "city":     ["city light", "city view", "skyline", "panoramic"],
}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
active_coll = db.unified_listings
closed_coll = db.unified_closed_listings
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 SHARED HELPERS (mirror build-subdivision-cma.py where applicable)
# ──────────────────────────────────────────────────────────────────────────────


def coerce_date(v):
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
    if pt and pt != RESIDENTIAL_PROPERTY_TYPE:
        return True
    if (listing.get("propertyTypeName") or "") == "Residential Lease":
        return True
    land = (listing.get("landType") or "").strip().lower()
    if "lease" in land:
        return True
    return False


def days_on_market(listing: dict, reference: datetime | None = None) -> int | None:
    dom = listing.get("daysOnMarket")
    if isinstance(dom, (int, float)) and dom >= 0:
        return int(dom)
    omd = coerce_date(listing.get("onMarketDate")) or coerce_date(listing.get("listingContractDate"))
    if not omd:
        return None
    end = reference or coerce_date(listing.get("closeDate")) or datetime.now(timezone.utc)
    delta = (end - omd).days
    return delta if delta >= 0 else None


def _escape_regex(s: str) -> str:
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)


def _round_int(v):
    try:
        return int(round(v))
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _safe_float(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _ppsf(price, sqft):
    if not price or not sqft or sqft <= 0:
        return None
    return price / sqft


def _normalize_address(listing: dict) -> str:
    parts = [
        listing.get("streetNumber"),
        listing.get("streetName"),
        listing.get("streetSuffix"),
    ]
    line = " ".join(str(p) for p in parts if p)
    if not line:
        line = listing.get("unparsedAddress") or listing.get("address") or ""
    return line.strip()


# ──────────────────────────────────────────────────────────────────────────────
# 🏷️ ATTRIBUTE DETECTION (MLS fields + remarks parsing)
# ──────────────────────────────────────────────────────────────────────────────

# Keep negation patterns global so they're compiled once.
_RE_NO_POOL = re.compile(r"\b(no pool|pool removed|without (?:a )?pool)\b", re.I)
_RE_COMMUNITY_POOL = re.compile(r"\b(community|hoa|shared|association)\s+pool\b", re.I)
_RE_POOL_TABLE = re.compile(r"\bpool table\b|\bcarpool\b|\bpool house\b", re.I)
_RE_POOL_POS = re.compile(
    r"\b("
    r"private pool|heated pool|swimming pool|pool and spa|pool/spa|"
    r"saltwater pool|pebble[ -]?tec pool|infinity pool|in[- ]?ground pool|"
    r"pool/?[\s-]?spa|sparkling pool|own(?:s|ed) (?:a )?pool|with (?:a )?pool"
    r")\b",
    re.I,
)

_RE_NO_SPA = re.compile(r"\bno (?:spa|hot tub|jacuzzi)\b", re.I)
_RE_SPA_LIKE = re.compile(r"\bspa[- ]?like|day spa\b", re.I)
_RE_SPA_POS = re.compile(
    r"\b(hot tub|jacuzzi|private spa|pool and spa|pool/spa|attached spa)\b",
    re.I,
)

_RE_GATED = re.compile(
    r"\b(gated community|guard[- ]?gated|24[- ]?hour guard|24/7 guard|gate guarded|manned gate)\b",
    re.I,
)
_RE_GOLF = re.compile(
    r"\b(golf membership|on the golf course|golf course (?:lot|view|frontage)|"
    r"fairway (?:lot|view)|golf community)\b",
    re.I,
)

_RE_GARAGE_COUNT = re.compile(
    r"\b("
    r"(\d)[- ]?car garage|garage for (\d)|(\d)[- ]?bay garage|"
    r"(\d)[- ]?stall garage|attached (\d)[- ]?car"
    r")\b",
    re.I,
)


def _parse_pool(remarks: str) -> bool | None:
    if not remarks:
        return None
    if _RE_NO_POOL.search(remarks):
        return False
    txt = _RE_COMMUNITY_POOL.sub("", remarks)
    txt = _RE_POOL_TABLE.sub("", txt)
    if _RE_POOL_POS.search(txt):
        return True
    return None


def _parse_spa(remarks: str) -> bool | None:
    if not remarks:
        return None
    if _RE_NO_SPA.search(remarks):
        return False
    txt = _RE_SPA_LIKE.sub("", remarks)
    if _RE_SPA_POS.search(txt):
        return True
    return None


def _parse_view(remarks: str) -> str | None:
    if not remarks:
        return None
    lower = remarks.lower()
    for cat, patterns in VIEW_CATEGORIES.items():
        if any(p in lower for p in patterns):
            return cat
    return None


def _parse_garage_count(remarks: str) -> int | None:
    if not remarks:
        return None
    m = _RE_GARAGE_COUNT.search(remarks)
    if not m:
        return None
    for g in m.groups()[1:]:  # skip the outer alternation group
        if g and g.isdigit():
            n = int(g)
            if 0 < n < 12:
                return n
    return None


def _categorize_view(view_value) -> str | None:
    """Map an MLS `view` string field onto one of our VIEW_CATEGORIES keys."""
    if not view_value:
        return None
    s = str(view_value).lower()
    for cat, patterns in VIEW_CATEGORIES.items():
        if any(p in s for p in patterns):
            return cat
    # Some MLSs use bare keywords like "Mountain", "Pool", "Desert"
    for cat in VIEW_CATEGORIES:
        if cat in s:
            return cat
    return None


def detect_attributes(listing: dict) -> dict:
    """Resolve pool/spa/view/garage/etc. — MLS fields first, remarks fallback."""
    remarks = (listing.get("publicRemarks") or "") + " " + (listing.get("privateRemarks") or "")

    pool = listing.get("poolYn")
    if pool is None:
        pool = listing.get("pool")
    if pool is None:
        pool = _parse_pool(remarks)

    spa = listing.get("spaYn")
    if spa is None:
        spa = listing.get("spa")
    if spa is None:
        spa = _parse_spa(remarks)

    view = _categorize_view(listing.get("view"))
    if view is None and listing.get("viewYn") is True:
        view = _parse_view(remarks)
    if view is None:
        view = _parse_view(remarks)

    garage = _safe_int(listing.get("garageSpaces"))
    if garage is None:
        garage = _parse_garage_count(remarks)
    garage = garage if garage is not None else 0

    gated = listing.get("gatedCommunityYn")
    if gated is None and _RE_GATED.search(remarks):
        gated = True

    senior = listing.get("seniorCommunityYn")

    golf = bool(_RE_GOLF.search(remarks))

    arch_style = listing.get("architecturalStyle")
    if isinstance(arch_style, list):
        arch_style = ", ".join(str(s) for s in arch_style if s)
    elif isinstance(arch_style, dict):
        arch_style = ", ".join(str(v) for v in arch_style.values() if v)
    elif arch_style is not None and not isinstance(arch_style, str):
        arch_style = str(arch_style)
    arch_style = (arch_style or "").strip() or None

    # `levels` / `stories` — RESO uses `levels`; some MLSs include `stories`.
    stories = listing.get("stories")
    if stories is None:
        levels = listing.get("levels")
        if isinstance(levels, list):
            levels = levels[0] if levels else None
        if isinstance(levels, str):
            m = re.search(r"\d+", levels)
            stories = int(m.group()) if m else None
    stories = _safe_int(stories)

    land_type = (listing.get("landType") or "").strip() or None

    lot_size = listing.get("lotSizeSquareFeet") or listing.get("lotSizeArea")
    if isinstance(lot_size, str):
        try:
            lot_size = float(lot_size)
        except ValueError:
            lot_size = None
    lot_size = _safe_int(lot_size) or 0

    return {
        "pool": pool,
        "spa": spa,
        "view": view,
        "garageSpaces": garage,
        "gated": gated,
        "senior": senior,
        "golf": golf,
        "archStyle": arch_style,
        "stories": stories,
        "landType": land_type,
        "lotSize": lot_size,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🪜 TIER + SUBJECT
# ──────────────────────────────────────────────────────────────────────────────


def classify_tier(list_price, living_area) -> str:
    lp = list_price or 0
    la = living_area or 0
    if lp > 1_500_000 or la > 3000:
        return "luxury"
    if lp < 500_000 or la < 1500:
        return "affordable"
    return "residential"


def build_subject(listing: dict) -> dict:
    attrs = detect_attributes(listing)
    list_price = _safe_int(listing.get("listPrice")) or 0
    living_area = _safe_int(listing.get("livingArea")) or 0
    beds = _safe_int(listing.get("bedsTotal")) or _safe_int(listing.get("bedroomsTotal")) or 0
    baths = (
        _safe_float(listing.get("bathsTotal"))
        or _safe_float(listing.get("bathroomsTotalDecimal"))
        or _safe_float(listing.get("bathroomsTotalInteger"))
        or 0
    )
    coords = listing.get("coordinates") or {}
    coords_arr = coords.get("coordinates") if isinstance(coords, dict) else None
    longitude = coords_arr[0] if isinstance(coords_arr, (list, tuple)) and len(coords_arr) == 2 else listing.get("longitude")
    latitude = coords_arr[1] if isinstance(coords_arr, (list, tuple)) and len(coords_arr) == 2 else listing.get("latitude")

    subject = {
        "listingKey": listing.get("listingKey"),
        "city": listing.get("city") or "",
        "subdivisionName": (listing.get("subdivisionName") or "").strip(),
        "propertyType": listing.get("propertyType") or RESIDENTIAL_PROPERTY_TYPE,
        "propertySubType": listing.get("propertySubType") or "",
        "listPrice": list_price,
        "livingArea": living_area,
        "lotSize": attrs["lotSize"],
        "bedsTotal": beds,
        "bathsTotal": round(baths, 2) if baths else 0,
        "yearBuilt": _safe_int(listing.get("yearBuilt")) or 0,
        "pricePerSqft": _round_int(_ppsf(list_price, living_area)) or 0,
        "landType": attrs["landType"] or "Fee",
        "pool": attrs["pool"],
        "spa": attrs["spa"],
        "view": attrs["view"],
        "garageSpaces": attrs["garageSpaces"],
        "archStyle": attrs["archStyle"],
        "stories": attrs["stories"],
        "longitude": longitude,
        "latitude": latitude,
        "tier": classify_tier(list_price, living_area),
    }
    return subject


# ──────────────────────────────────────────────────────────────────────────────
# 🔎 5-LEVEL SEARCH
# ──────────────────────────────────────────────────────────────────────────────


def _date_floor(months_back: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=months_back * 30)


def _build_query(level: int, subject: dict, status: str, params: dict) -> dict | None:
    """
    Build a Mongo query for a given (level, status). Returns None when the level
    cannot be applied (e.g., L1/L2 require a subdivisionName, L5 requires coords).
    """
    sqft = subject["livingArea"]
    sqft_range = params["sqftRange"]
    months = params["lookbackMonths"][level]

    base: dict = {
        "listingKey": {"$ne": subject["listingKey"]},
        "propertyType": subject["propertyType"],
    }
    if subject["propertySubType"]:
        base["propertySubType"] = subject["propertySubType"]

    if status == "active":
        base["standardStatus"] = {"$in": ACTIVE_STATUSES}
    else:
        base["closeDate"] = {"$gte": _date_floor(months).isoformat()}

    # Most listings carry sqft; if not, skip the constraint to avoid zero-result queries.
    if sqft and sqft > 0:
        base["livingArea"] = {"$gte": max(1, sqft - sqft_range), "$lte": sqft + sqft_range}

    if level == 1:
        # Skip subdivision-tight if the subject has no real subdivision (placeholder
        # names like "Not Applicable" exist in many cities and pull cross-city
        # comps), or if subject city is missing.
        if not _is_real_subdivision(subject["subdivisionName"]) or not subject["city"]:
            return None
        # Scope by BOTH subdivisionName AND city — common subdivision names
        # (e.g. "Bella Vista", "Trilogy") exist in many cities and would
        # otherwise pull cross-city comps. Regression caught 2026-05-05.
        base["subdivisionName"] = {"$regex": f"^{_escape_regex(subject['subdivisionName'])}$", "$options": "i"}
        base["city"] = {"$regex": f"^{_escape_regex(subject['city'])}$", "$options": "i"}
        if subject["bedsTotal"]:
            base["bedsTotal"] = {"$gte": subject["bedsTotal"] - 1, "$lte": subject["bedsTotal"] + 1}
        if subject["landType"]:
            base["landType"] = subject["landType"]
        if subject["pool"] is True:
            base["$or"] = [{"poolYn": True}, {"pool": True}]

    elif level == 2:
        if not _is_real_subdivision(subject["subdivisionName"]) or not subject["city"]:
            return None
        base["subdivisionName"] = {"$regex": f"^{_escape_regex(subject['subdivisionName'])}$", "$options": "i"}
        base["city"] = {"$regex": f"^{_escape_regex(subject['city'])}$", "$options": "i"}
        if sqft and sqft > 0:
            base["livingArea"] = {"$gte": max(1, sqft - sqft_range - 200), "$lte": sqft + sqft_range + 200}

    elif level == 3:
        if not subject["city"]:
            return None
        base["city"] = {"$regex": f"^{_escape_regex(subject['city'])}$", "$options": "i"}
        if subject["bedsTotal"]:
            base["bedsTotal"] = {"$gte": subject["bedsTotal"] - 1, "$lte": subject["bedsTotal"] + 1}
        if subject["pool"] is True:
            base["$or"] = [{"poolYn": True}, {"pool": True}]

    elif level == 4:
        if not subject["city"]:
            return None
        base["city"] = {"$regex": f"^{_escape_regex(subject['city'])}$", "$options": "i"}
        if sqft and sqft > 0:
            base["livingArea"] = {"$gte": max(1, sqft - sqft_range - 300), "$lte": sqft + sqft_range + 300}

    elif level == 5:
        lng, lat = subject["longitude"], subject["latitude"]
        if not (isinstance(lng, (int, float)) and isinstance(lat, (int, float))):
            return None
        max_dist_m = int(params["radiusFallbackMiles"] * 1609.34)
        base["coordinates"] = {
            "$nearSphere": {
                "$geometry": {"type": "Point", "coordinates": [lng, lat]},
                "$maxDistance": max_dist_m,
            }
        }

    return base


def _query_collection(coll, query: dict, limit: int) -> list[dict]:
    cursor = coll.find(query, COMP_PROJECTION).limit(limit)
    return [doc for doc in cursor if not is_lease(doc)]


# ──────────────────────────────────────────────────────────────────────────────
# 📊 12-FACTOR SCORING
# ──────────────────────────────────────────────────────────────────────────────


def _score_sqft(subject, comp):
    s, c = subject["livingArea"], _safe_int(comp.get("livingArea"))
    if not s or not c:
        return None  # non-applicable
    return max(0.0, 1.0 - abs(c - s) / s)


def _score_subdivision(subject, comp):
    s = (subject["subdivisionName"] or "").strip().lower()
    if not s or s in PLACEHOLDER_SUBDIVISIONS:
        return None  # non-applicable; factor weight redistributes
    c = (comp.get("subdivisionName") or "").strip().lower()
    return 1.0 if s == c else 0.0


def _score_bed_bath(subject, comp):
    sb, sB = subject["bedsTotal"], subject["bathsTotal"]
    cb = _safe_int(comp.get("bedsTotal")) or _safe_int(comp.get("bedroomsTotal")) or 0
    cB = (
        _safe_float(comp.get("bathsTotal"))
        or _safe_float(comp.get("bathroomsTotalDecimal"))
        or _safe_float(comp.get("bathroomsTotalInteger"))
        or 0
    )

    def step(diff):
        d = abs(diff)
        if d == 0:
            return 1.0
        if d <= 1:
            return 0.65
        if d <= 2:
            return 0.30
        return 0.0

    if not sb and not sB:
        return None
    parts = []
    if sb:
        parts.append(step(cb - sb))
    if sB:
        parts.append(step(cB - sB))
    return sum(parts) / len(parts) if parts else None


def _comp_pool(comp):
    """Comp-side pool resolution from MLS fields only — no remarks parsing (hot path)."""
    p = comp.get("poolYn")
    if p is None:
        p = comp.get("pool")
    return p if isinstance(p, bool) else None


def _comp_spa(comp):
    s = comp.get("spaYn")
    if s is None:
        s = comp.get("spa")
    return s if isinstance(s, bool) else None


def _score_pool_spa(subject, comp):
    s_pool, s_spa = subject["pool"], subject["spa"]
    c_pool, c_spa = _comp_pool(comp), _comp_spa(comp)

    parts = []
    if s_pool is not None:
        if c_pool is None:
            parts.append(0.5)
        else:
            parts.append(1.0 if c_pool == s_pool else 0.0)
    if s_spa is not None:
        if c_spa is None:
            parts.append(0.5)
        else:
            parts.append(1.0 if c_spa == s_spa else 0.0)

    if not parts:
        return None
    return sum(parts) / len(parts)


def _score_lot_size(subject, comp):
    s = subject["lotSize"]
    c = comp.get("lotSizeSquareFeet") or comp.get("lotSizeArea") or 0
    if isinstance(c, str):
        try:
            c = float(c)
        except ValueError:
            c = 0
    if not s or not c:
        return None
    return max(0.0, 1.0 - abs(c - s) / s)


def _score_view(subject, comp):
    s = subject["view"]
    if not s:
        return None
    # Comp-side: MLS field only — remarks parsing is too expensive on the hot path.
    c = _categorize_view(comp.get("view"))
    if not c:
        return 0.2
    return 1.0 if c == s else 0.1


def _score_recency(comp, status, max_lookback_months):
    if status == "closed":
        cd = coerce_date(comp.get("closeDate"))
    else:
        cd = coerce_date(comp.get("onMarketDate")) or coerce_date(comp.get("listingContractDate"))
    if not cd:
        return None
    months_ago = (datetime.now(timezone.utc) - cd).days / 30.0
    return max(0.0, 1.0 - months_ago / max(1, max_lookback_months))


def _score_year_built(subject, comp):
    s = subject["yearBuilt"]
    c = _safe_int(comp.get("yearBuilt"))
    if not s and not c:
        return None
    if not s or not c:
        return 0.5
    return max(0.0, 1.0 - abs(c - s) / 30.0)


def _score_arch_style(subject, comp):
    s = subject["archStyle"]
    if not s:
        return None
    c = comp.get("architecturalStyle")
    if isinstance(c, list):
        c = ", ".join(str(x) for x in c if x)
    elif isinstance(c, dict):
        c = ", ".join(str(v) for v in c.values() if v)
    elif c is not None and not isinstance(c, str):
        c = str(c)
    if not c:
        return 0.0
    s_words = set(re.findall(r"[a-z]+", s.lower()))
    c_words = set(re.findall(r"[a-z]+", c.lower()))
    if not s_words:
        return None
    overlap = len(s_words & c_words) / len(s_words)
    return min(1.0, overlap)


def _score_stories(subject, comp):
    s = subject["stories"]
    if not s:
        return None
    c = _safe_int(comp.get("stories"))
    if c is None:
        levels = comp.get("levels")
        if isinstance(levels, list) and levels:
            levels = levels[0]
        if isinstance(levels, str):
            m = re.search(r"\d+", levels)
            c = int(m.group()) if m else None
    if c is None:
        return 0.5
    return 1.0 if c == s else 0.3


def _score_garage(subject, comp):
    s = subject["garageSpaces"]
    if s is None:
        return None
    c = _safe_int(comp.get("garageSpaces"))
    if c is None:
        return 0.5
    diff = abs(c - s)
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.6
    return 0.2


def _score_land_type(subject, comp):
    s = (subject["landType"] or "").strip().lower()
    c = ((comp.get("landType") or "").strip().lower())
    if not s and not c:
        return None
    if not s or not c:
        return 0.5
    return 1.0 if s == c else 0.0


SCORERS = {
    "sqft":        lambda s, c, st, mlm: _score_sqft(s, c),
    "subdivision": lambda s, c, st, mlm: _score_subdivision(s, c),
    "bedBath":     lambda s, c, st, mlm: _score_bed_bath(s, c),
    "poolSpa":     lambda s, c, st, mlm: _score_pool_spa(s, c),
    "lotSize":     lambda s, c, st, mlm: _score_lot_size(s, c),
    "view":        lambda s, c, st, mlm: _score_view(s, c),
    "recency":     lambda s, c, st, mlm: _score_recency(c, st, mlm),
    "yearBuilt":   lambda s, c, st, mlm: _score_year_built(s, c),
    "archStyle":   lambda s, c, st, mlm: _score_arch_style(s, c),
    "stories":     lambda s, c, st, mlm: _score_stories(s, c),
    "garage":      lambda s, c, st, mlm: _score_garage(s, c),
    "landType":    lambda s, c, st, mlm: _score_land_type(s, c),
}


def score_comp(subject: dict, comp: dict, status: str, max_lookback_months: int) -> float:
    weights = SCORING_WEIGHTS[subject["tier"]]
    raw_scores: dict[str, float] = {}
    applicable_weight = 0.0
    for factor, fn in SCORERS.items():
        s = fn(subject, comp, status, max_lookback_months)
        if s is None:
            continue
        raw_scores[factor] = max(0.0, min(1.0, s))
        applicable_weight += weights.get(factor, 0)

    if applicable_weight == 0:
        return 0.0

    # Redistribute proportionally so the applicable weights sum to 100.
    scale = 100.0 / applicable_weight
    total = sum(raw_scores[f] * weights[f] * scale for f in raw_scores)
    return round(total, 2)


# ──────────────────────────────────────────────────────────────────────────────
# 🧱 SEARCH ORCHESTRATION
# ──────────────────────────────────────────────────────────────────────────────


def search_for_status(subject: dict, status: str, params: dict) -> tuple[list[tuple[float, dict]], int, int, int]:
    """
    Walk levels 1→5 for a single status. Stop at the first level that yields
    ≥ MIN_COMPS_PER_STATUS scored comps. Return (scored_comps, level_used,
    candidates_evaluated, subdivision_matched_flag).
    """
    coll = active_coll if status == "active" else closed_coll
    candidates_evaluated = 0
    last_level = 0
    subdivision_matched = 0  # 1 if level 1 or 2 produced the comps

    for level in range(1, 6):
        query = _build_query(level, subject, status, params)
        if query is None:
            continue
        max_lookback = params["lookbackMonths"][5]  # widest possible window for recency scoring
        candidates = _query_collection(coll, query, LEVEL_CANDIDATE_LIMIT)
        candidates_evaluated += len(candidates)
        last_level = level

        scored = []
        for c in candidates:
            score = score_comp(subject, c, status, max_lookback)
            scored.append((score, c))
        scored.sort(key=lambda r: r[0], reverse=True)

        if len(scored) >= MIN_COMPS_PER_STATUS:
            if level <= 2:
                subdivision_matched = 1
            return scored[: params["maxCompsPerStatus"]], level, candidates_evaluated, subdivision_matched

    # Never met the threshold — return whatever we last scored.
    return scored[: params["maxCompsPerStatus"]] if "scored" in locals() else [], last_level, candidates_evaluated, subdivision_matched


# ──────────────────────────────────────────────────────────────────────────────
# 📦 SHAPE THE OUTPUT
# ──────────────────────────────────────────────────────────────────────────────


def _shape_active_comp(score: float, c: dict) -> dict:
    list_price = _safe_int(c.get("listPrice")) or 0
    living_area = _safe_int(c.get("livingArea")) or 0
    return {
        "listingKey": c.get("listingKey"),
        "address": _normalize_address(c),
        "city": c.get("city") or "",
        "subdivisionName": c.get("subdivisionName") or None,
        "listPrice": list_price,
        "livingArea": living_area,
        "bedsTotal": _safe_int(c.get("bedsTotal")) or 0,
        "bathsTotal": _safe_float(c.get("bathsTotal")) or _safe_float(c.get("bathroomsTotalDecimal")) or 0,
        "yearBuilt": _safe_int(c.get("yearBuilt")) or 0,
        "listPricePerSqft": _round_int(_ppsf(list_price, living_area)) or 0,
        "daysOnMarket": days_on_market(c) or 0,
        "similarityScore": score,
        "pool": _comp_pool(c),
        "spa": _comp_spa(c),
        "view": _categorize_view(c.get("view")),
        "garageSpaces": _safe_int(c.get("garageSpaces")) or 0,
        "landType": (c.get("landType") or "").strip() or "Fee",
        "lotSize": _safe_int(c.get("lotSizeSquareFeet")) or _safe_int(c.get("lotSizeArea")) or 0,
    }


def _shape_closed_comp(score: float, c: dict) -> dict:
    close_price = _safe_int(c.get("closePrice")) or 0
    list_price = _safe_int(c.get("listPrice")) or _safe_int(c.get("originalListPrice")) or 0
    living_area = _safe_int(c.get("livingArea")) or 0
    cd = coerce_date(c.get("closeDate"))
    stl = (close_price / list_price) if (close_price and list_price) else None
    return {
        "listingKey": c.get("listingKey"),
        "address": _normalize_address(c),
        "city": c.get("city") or "",
        "subdivisionName": c.get("subdivisionName") or None,
        "closePrice": close_price,
        "closeDate": cd.date().isoformat() if cd else None,
        "listPrice": list_price,
        "livingArea": living_area,
        "bedsTotal": _safe_int(c.get("bedsTotal")) or 0,
        "bathsTotal": _safe_float(c.get("bathsTotal")) or _safe_float(c.get("bathroomsTotalDecimal")) or 0,
        "yearBuilt": _safe_int(c.get("yearBuilt")) or 0,
        "salePricePerSqft": _round_int(_ppsf(close_price, living_area)) or 0,
        "salePriceToListRatio": round(stl, 3) if stl else None,
        "daysOnMarket": days_on_market(c, cd) or 0,
        "similarityScore": score,
        "pool": _comp_pool(c),
        "spa": _comp_spa(c),
        "view": _categorize_view(c.get("view")),
        "garageSpaces": _safe_int(c.get("garageSpaces")) or 0,
        "landType": (c.get("landType") or "").strip() or "Fee",
        "lotSize": _safe_int(c.get("lotSizeSquareFeet")) or _safe_int(c.get("lotSizeArea")) or 0,
    }


def _aggregate_active(comps: list[dict]) -> dict:
    if not comps:
        return {"count": 0, "avgPrice": 0, "minPrice": 0, "maxPrice": 0, "medianPrice": 0,
                "avgPricePerSqft": 0, "avgSqft": 0, "avgDaysOnMarket": 0, "avgLotSize": 0}
    prices = [c["listPrice"] for c in comps if c["listPrice"]]
    ppsfs = [c["listPricePerSqft"] for c in comps if c["listPricePerSqft"]]
    sqfts = [c["livingArea"] for c in comps if c["livingArea"]]
    doms = [c["daysOnMarket"] for c in comps if c.get("daysOnMarket") is not None]
    lots = [c["lotSize"] for c in comps if c["lotSize"]]
    return {
        "count": len(comps),
        "avgPrice": _round_int(statistics.mean(prices)) or 0,
        "minPrice": min(prices) if prices else 0,
        "maxPrice": max(prices) if prices else 0,
        "medianPrice": _round_int(statistics.median(prices)) or 0 if prices else 0,
        "avgPricePerSqft": _round_int(statistics.mean(ppsfs)) or 0 if ppsfs else 0,
        "avgSqft": _round_int(statistics.mean(sqfts)) or 0 if sqfts else 0,
        "avgDaysOnMarket": _round_int(statistics.mean(doms)) or 0 if doms else 0,
        "avgLotSize": _round_int(statistics.mean(lots)) or 0 if lots else 0,
    }


def _aggregate_closed(comps: list[dict]) -> dict:
    if not comps:
        return {"count": 0, "avgPrice": 0, "minPrice": 0, "maxPrice": 0, "medianPrice": 0,
                "avgPricePerSqft": 0, "avgSqft": 0, "avgDaysOnMarket": 0, "avgLotSize": 0,
                "avgSalePriceToListRatio": None}
    prices = [c["closePrice"] for c in comps if c["closePrice"]]
    ppsfs = [c["salePricePerSqft"] for c in comps if c["salePricePerSqft"]]
    sqfts = [c["livingArea"] for c in comps if c["livingArea"]]
    doms = [c["daysOnMarket"] for c in comps if c.get("daysOnMarket") is not None]
    lots = [c["lotSize"] for c in comps if c["lotSize"]]
    ratios = [c["salePriceToListRatio"] for c in comps if c.get("salePriceToListRatio")]
    return {
        "count": len(comps),
        "avgPrice": _round_int(statistics.mean(prices)) or 0,
        "minPrice": min(prices) if prices else 0,
        "maxPrice": max(prices) if prices else 0,
        "medianPrice": _round_int(statistics.median(prices)) or 0 if prices else 0,
        "avgPricePerSqft": _round_int(statistics.mean(ppsfs)) or 0 if ppsfs else 0,
        "avgSqft": _round_int(statistics.mean(sqfts)) or 0 if sqfts else 0,
        "avgDaysOnMarket": _round_int(statistics.mean(doms)) or 0 if doms else 0,
        "avgLotSize": _round_int(statistics.mean(lots)) or 0 if lots else 0,
        "avgSalePriceToListRatio": round(statistics.mean(ratios), 3) if ratios else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 📝 NARRATIVE / LIMITATIONS / QUALITY
# ──────────────────────────────────────────────────────────────────────────────


def generate_narrative(subject: dict, active_stats: dict, closed_stats: dict, levels_used: dict) -> str:
    parts: list[str] = []

    median_closed = closed_stats.get("medianPrice") or 0
    if median_closed and subject["listPrice"]:
        diff_pct = (subject["listPrice"] - median_closed) / median_closed * 100
        if abs(diff_pct) >= 3:
            direction = "above" if diff_pct > 0 else "below"
            parts.append(
                f"Listed {abs(diff_pct):.1f}% {direction} the median closed sale of ${median_closed:,.0f}."
            )
        else:
            parts.append(f"Listed in line with the median closed sale of ${median_closed:,.0f}.")

    subj_ppsf = subject.get("pricePerSqft") or 0
    closed_ppsf = closed_stats.get("avgPricePerSqft") or 0
    if subj_ppsf and closed_ppsf:
        ppsf_diff = (subj_ppsf - closed_ppsf) / closed_ppsf * 100
        if abs(ppsf_diff) >= 10:
            direction = "premium to" if ppsf_diff > 0 else "discount to"
            parts.append(
                f"Price per sqft (${subj_ppsf:,}) is a {abs(ppsf_diff):.0f}% {direction} closed comps (${closed_ppsf:,})."
            )

    stl = closed_stats.get("avgSalePriceToListRatio")
    if stl is not None:
        if stl < 0.95:
            parts.append(f"Closed comps sold at {stl:.0%} of list — buyers have negotiating leverage.")
        elif stl > 1.0:
            parts.append(f"Closed comps sold at {stl:.0%} of list — strong seller's market.")

    avg_dom = closed_stats.get("avgDaysOnMarket") or 0
    if avg_dom:
        if avg_dom <= 30:
            parts.append(f"Comparable homes are moving quickly (avg {avg_dom} days on market).")
        elif avg_dom >= 120:
            parts.append(f"Expect a longer timeline — comps averaged {avg_dom} days on market.")

    if subject["tier"] == "luxury":
        parts.append("Luxury tier — fewer comparable sales widen the confidence range.")

    if not parts:
        return "Comparable sales support the current list price."
    return " ".join(parts)


def collect_limitations(subject: dict, active_comps: list[dict], closed_comps: list[dict],
                       levels_used: dict, subdivision_matched: bool) -> list[str]:
    notes: list[str] = []

    if len(active_comps) < 3:
        notes.append(f"Only {len(active_comps)} active comparable(s) found.")
    if len(closed_comps) < 3:
        notes.append(f"Only {len(closed_comps)} closed comparable(s) found.")

    if subject["subdivisionName"] and not subdivision_matched:
        notes.append(f"Insufficient comparables in {subject['subdivisionName']} — search expanded beyond the subdivision.")

    if subject["pool"] is True and closed_comps:
        with_pool = sum(1 for c in closed_comps if c.get("pool") is True)
        if with_pool / len(closed_comps) < 0.6:
            notes.append("Pool mismatch: subject has a pool but most closed comps do not.")

    if levels_used.get("closed", 0) >= 4:
        notes.append("Closed comps required an extended lookback period.")

    subject_lease = (subject["landType"] or "").lower() == "lease"
    if not subject_lease and closed_comps:
        lease_comps = sum(1 for c in closed_comps if (c.get("landType") or "").lower() == "lease")
        if lease_comps:
            notes.append(f"Land-type mismatch: {lease_comps} of {len(closed_comps)} closed comps are lease-land.")

    return notes


def collect_inferences(subject: dict, listing: dict) -> list[str]:
    """Note attributes that were inferred from public remarks rather than read from MLS fields."""
    notes: list[str] = []
    has_field_pool = listing.get("poolYn") is not None or listing.get("pool") is not None
    if subject["pool"] is not None and not has_field_pool:
        notes.append(f"Pool inferred from listing remarks ({'present' if subject['pool'] else 'absent'}).")
    has_field_spa = listing.get("spaYn") is not None or listing.get("spa") is not None
    if subject["spa"] is not None and not has_field_spa:
        notes.append(f"Spa inferred from listing remarks ({'present' if subject['spa'] else 'absent'}).")
    if subject["view"] and not listing.get("view") and not (listing.get("viewYn") is True):
        notes.append(f"View category '{subject['view']}' inferred from listing remarks.")
    if subject["garageSpaces"] and listing.get("garageSpaces") in (None, 0):
        notes.append(f"Garage spaces ({subject['garageSpaces']}) inferred from listing remarks.")
    return notes


def assess_quality(active_count: int, closed_count: int) -> dict:
    total = active_count + closed_count
    if total >= 8:
        confidence = "high"
    elif total >= 5:
        confidence = "good"
    elif total >= 3:
        confidence = "medium"
    elif total >= 1:
        confidence = "low"
    else:
        confidence = "insufficient"
    return {"confidence": confidence, "activeCompCount": active_count, "closedCompCount": closed_count}


# ──────────────────────────────────────────────────────────────────────────────
# 🧮 PER-LISTING BUILDER
# ──────────────────────────────────────────────────────────────────────────────


def build_cma_for_listing(listing: dict) -> dict:
    subject = build_subject(listing)
    tier = subject["tier"]
    params = TIER_PARAMS[tier]

    active_scored, active_level, active_evaluated, active_subdiv = search_for_status(subject, "active", params)
    closed_scored, closed_level, closed_evaluated, closed_subdiv = search_for_status(subject, "closed", params)

    active_comps = [_shape_active_comp(score, c) for score, c in active_scored]
    closed_comps = [_shape_closed_comp(score, c) for score, c in closed_scored]

    active_stats = _aggregate_active(active_comps)
    closed_stats = _aggregate_closed(closed_comps)

    levels_used = {"active": active_level, "closed": closed_level}
    subdivision_matched = bool(active_subdiv or closed_subdiv)

    quality = assess_quality(len(active_comps), len(closed_comps))
    narrative = generate_narrative(subject, active_stats, closed_stats, levels_used)
    limitations = collect_limitations(subject, active_comps, closed_comps, levels_used, subdivision_matched)
    inferences = collect_inferences(subject, listing)

    # `subject` includes some convenience fields (e.g. tier, longitude/latitude).
    # The persisted shape keeps only the spec-required keys.
    subject_out = {
        "listPrice": subject["listPrice"],
        "livingArea": subject["livingArea"],
        "lotSize": subject["lotSize"],
        "bedsTotal": subject["bedsTotal"],
        "bathsTotal": subject["bathsTotal"],
        "yearBuilt": subject["yearBuilt"],
        "pricePerSqft": subject["pricePerSqft"],
        "propertyType": subject["propertyType"],
        "propertySubType": subject["propertySubType"],
        "landType": subject["landType"],
        "pool": subject["pool"],
        "spa": subject["spa"],
        "view": subject["view"],
        "garageSpaces": subject["garageSpaces"],
    }

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "tier": tier,
        "subject": subject_out,
        "activeComps": active_comps,
        "closedComps": closed_comps,
        "stats": {"active": active_stats, "closed": closed_stats},
        "searchCriteria": {
            "levelsUsed": levels_used,
            "subdivisionMatched": subdivision_matched,
            "totalCandidatesEvaluated": {"active": active_evaluated, "closed": closed_evaluated},
        },
        "narrative": narrative,
        "limitations": limitations,
        "inferences": inferences,
        "quality": quality,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🛠️ WORKER (single chunk of listing _ids — runs inline or in a child process)
# ──────────────────────────────────────────────────────────────────────────────


def _process_listing_chunk(listing_ids: list, options: dict) -> dict:
    """
    Process a list of listing _ids and return aggregate counts. Module-level
    `active_coll` / `closed_coll` are reused — when this is invoked in a spawned
    child process, the module's top-level code re-runs and gives that child its
    own MongoClient (the recommended pymongo+multiprocessing pattern).
    """
    pending: list[UpdateOne] = []
    written = processed = skipped_fresh = failed = 0
    confidence_counts: Counter = Counter()

    fresh_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=options["skip_fresh_hours"])
        if options["skip_fresh_hours"] > 0 and not options.get("listing_key") else None
    )
    label_prefix = options.get("worker_label", "")
    dry_run = bool(options.get("dry_run"))
    verbose = bool(options.get("verbose"))
    dump_first = bool(options.get("dump_first"))

    for _id in listing_ids:
        listing = active_coll.find_one({"_id": _id})
        if not listing:
            continue
        if fresh_cutoff is not None:
            existing = listing.get("cmaStats") or {}
            last_updated = coerce_date(existing.get("lastUpdated"))
            if last_updated and last_updated >= fresh_cutoff:
                skipped_fresh += 1
                continue
        processed += 1
        label = f"{listing.get('city','?')} / {(listing.get('subdivisionName') or '—')[:25]}"
        try:
            stats = build_cma_for_listing(listing)
        except Exception as e:
            failed += 1
            print(f"  [ERR{label_prefix}] {listing.get('listingKey')}: {e}", flush=True)
            if verbose:
                import traceback
                traceback.print_exc()
            continue

        confidence_counts[stats["quality"]["confidence"]] += 1
        active_n = stats["stats"]["active"]["count"]
        closed_n = stats["stats"]["closed"]["count"]
        median = stats["stats"]["closed"].get("medianPrice") or 0
        median_str = f"median ${median:,.0f}" if median else "median —"
        levels = stats["searchCriteria"]["levelsUsed"]

        print(
            f"{label_prefix}#{processed}: {label[:38]:<38} "
            f"{active_n:>2} active (L{levels['active']}), "
            f"{closed_n:>2} closed (L{levels['closed']}), "
            f"{median_str}  conf={stats['quality']['confidence']}",
            flush=True,
        )
        if verbose:
            print(f"      tier={stats['tier']} narrative={stats['narrative'][:120]}", flush=True)

        if dry_run and dump_first and processed == 1:
            print(json.dumps(stats, indent=2, default=str), flush=True)

        if not dry_run:
            pending.append(UpdateOne({"_id": _id}, {"$set": {"cmaStats": stats}}))
            if len(pending) >= BULK_BATCH:
                res = active_coll.bulk_write(pending, ordered=False)
                written += res.modified_count
                pending.clear()

    if not dry_run and pending:
        res = active_coll.bulk_write(pending, ordered=False)
        written += res.modified_count

    return {
        "processed": processed,
        "skipped_fresh": skipped_fresh,
        "failed": failed,
        "written": written,
        "confidence_counts": dict(confidence_counts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Pre-compute cmaStats on every active listing.")
    parser.add_argument("--all", action="store_true", help="Process every active listing.")
    parser.add_argument("--key", "--listing-key", dest="listing_key",
                        help="Process a single listing by listingKey.")
    parser.add_argument("--city", help="Process all active listings in this city.")
    parser.add_argument("--county", help="Process all active listings in this county (matches countyOrParish).")
    parser.add_argument("--keys-file", help="Path to a file containing one listingKey per line. "
                                            "Useful for targeted re-runs (e.g. fixing a regression).")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write.")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-listing logging.")
    parser.add_argument("--skip-fresh-hours", type=int, default=60,
                        help="Skip listings whose cmaStats.lastUpdated is younger than this. "
                             "Default 60h fits a twice-weekly schedule (every listing refreshed at "
                             "least every other run). Pass 0 to force a full rebuild.")
    parser.add_argument("--workers", type=int, default=1,
                        help="Run N parallel worker processes. 1 = single-threaded (default). "
                             "4–6 is the sweet spot vs. Atlas connection budget — more than 8 "
                             "tends to hit diminishing returns from latency contention.")
    args = parser.parse_args()

    if not (args.all or args.listing_key or args.city or args.county or args.keys_file):
        print("[ERROR] Must pass --all, --key <listingKey>, --city <city>, --county <county>, or --keys-file <path>.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    query: dict = {
        "propertyType": RESIDENTIAL_PROPERTY_TYPE,
        "standardStatus": {"$in": ACTIVE_STATUSES},
    }
    if args.listing_key:
        query = {"listingKey": args.listing_key}
    elif args.keys_file:
        with open(args.keys_file) as f:
            keys = [line.strip() for line in f if line.strip()]
        if not keys:
            print(f"[ERROR] No listingKeys in {args.keys_file}", file=sys.stderr)
            sys.exit(1)
        query = {"listingKey": {"$in": keys}}
        print(f"   Loaded {len(keys):,} listingKeys from {args.keys_file}")
    elif args.city:
        query["city"] = {"$regex": f"^{_escape_regex(args.city)}$", "$options": "i"}
    elif args.county:
        query["countyOrParish"] = {"$regex": f"^{_escape_regex(args.county)}$", "$options": "i"}

    # Pre-filter freshness at the cursor level so we don't pay to fetch fresh docs.
    fresh_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=args.skip_fresh_hours)
        if args.skip_fresh_hours > 0 and not args.listing_key else None
    )
    if fresh_cutoff is not None:
        query["$or"] = [
            {"cmaStats.lastUpdated": {"$exists": False}},
            {"cmaStats.lastUpdated": {"$lt": fresh_cutoff.isoformat()}},
        ]

    print(f"🏠 Counting eligible listings (dry-run={args.dry_run}, skip_fresh_hours={args.skip_fresh_hours})…", flush=True)
    id_cursor = active_coll.find(query, {"_id": 1}).batch_size(500)
    if args.limit:
        id_cursor = id_cursor.limit(args.limit)
    listing_ids = [doc["_id"] for doc in id_cursor]

    if not listing_ids:
        print("Nothing to do — no listings matched the query (or all were fresh).")
        return

    workers = max(1, args.workers)
    print(f"   {len(listing_ids):,} listings to process across {workers} worker(s)", flush=True)

    started = time.time()
    options_base = {
        "skip_fresh_hours": args.skip_fresh_hours,
        "listing_key": args.listing_key,
        "dry_run": args.dry_run,
        "verbose": args.verbose,
        "dump_first": args.dry_run,
    }

    if workers <= 1:
        result = _process_listing_chunk(listing_ids, {**options_base, "worker_label": ""})
        results = [result]
    else:
        # Round-robin shard so each worker gets a mix of fast/slow neighborhoods
        # rather than one worker drawing all the luxury (slow) cases.
        chunks = [listing_ids[i::workers] for i in range(workers)]
        chunks = [c for c in chunks if c]
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futures = [
                ex.submit(_process_listing_chunk, chunk, {**options_base, "worker_label": f"[w{i}] "})
                for i, chunk in enumerate(chunks)
            ]
            results = [f.result() for f in futures]

    total_processed = sum(r["processed"] for r in results)
    total_skipped = sum(r["skipped_fresh"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    total_written = sum(r["written"] for r in results)
    confidence_total: Counter = Counter()
    for r in results:
        for k, v in r["confidence_counts"].items():
            confidence_total[k] += v

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    print(f"Processed:     {total_processed:,}")
    print(f"Skipped fresh: {total_skipped:,}")
    print(f"Failed:        {total_failed:,}")
    print("Confidence distribution:")
    for conf in ("high", "good", "medium", "low", "insufficient"):
        if confidence_total[conf]:
            print(f"  {conf:>12s}: {confidence_total[conf]:,}")
    print(f"Written:       {total_written:,}" if not args.dry_run else "Dry-run (no writes)")
    print(f"Elapsed:       {elapsed/60:.1f} min ({elapsed:.0f}s)")
    if total_processed:
        wall_per_listing = elapsed / total_processed
        print(f"Wall/listing:  {wall_per_listing:.1f}s  (workers={workers})")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
