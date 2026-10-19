#!/usr/bin/env python3
"""
Build Closed-Lease Seasonal Rates  (P4.6 — layer 1.6 aggregation)

Seasonal going rates derived from leases that ACTUALLY CLOSED, as a second
source beside build-seasonal-rates.py's active-listing layer.

── Why this exists ───────────────────────────────────────────────────────────
build-seasonal-rates.py reads `rateProfile` off ACTIVE leases only, and only
~4.75% of those profiles come back "tiered", so the seasonal layer runs on
roughly 750 listings valley-wide. The result: 318 of 333 subdivisions are
"insufficient" for seasonal, and the PDCC showcase's headline 4-bedroom number
rests on 2 comps.

Meanwhile `unified_closed_listings` holds 254,809 closed leases going back to
2020-08-28 — 16x the active lease pool, and better evidence besides, since
closePrice is rent actually achieved rather than rent asked.

Note what this does NOT duplicate: build-rent-rates.py already reads closed
leases for the LONG-TERM going rate (24-month window, segmented by `furnished`).
The gap this script fills is seasonal specifically.

── How a seasonal RATE is identified ─────────────────────────────────────────
There is no lease-term or frequency field in the data. The selector is PRICE,
with the `furnished` field as corroboration — not the other way round.

The obvious rule, "furnished ⇒ seasonal", is wrong, and measurably so. Only
14.8% of furnished closed leases are priced anywhere near a peak-season rate;
the other 85% close at $3,450 against an unfurnished median of $3,250. Those
are furnished ANNUAL rentals. Selecting the cohort on the furnished flag puts
them in the seasonal median and collapses it: ZIP 92211 came out at $3,500
against a $2,800 long-term baseline — a 1.25x "seasonal lift" that describes
nothing anyone would call a season.

So:

1. SELECTOR — the "2x tail". Within a (ZIP, beds) cell holding at least 15
   closes, a close at or above 2x the cell's own median. That IS the seasonal
   rate: 92211 3bd resolves to $9,300 against a $3,200 baseline.

2. CORROBORATION — the `furnished` field, where present, splits the tail into
   "direct" and "inferred" evidence. The field is ~97% null across all 254,809
   closed leases but populated on 96% of GPS (Coachella Valley) closes, which
   is exactly the market where seasonal pricing matters. On the rows carrying
   both signals:

       furnished    n=1,894   14.8% land in the tail   tail median $10,000
       unfurnished  n=6,006    0.6% land in the tail   non-tail median $3,250

   A 25x enrichment. The tail is almost purely furnished inventory, which is
   what licenses using price alone where the flag is missing.

3. The long-term baseline takes non-tail closes with the furnished ones
   REMOVED, matching build-rent-rates.py's unfurnished basis. Furnished
   non-tail closes — the furnished-annual cohort — are counted and reported
   (`furnishedAnnualCount`) but join neither median, because they are neither
   a peak-season rate nor a clean annual one.

── What this CANNOT tell you, and why ────────────────────────────────────────
Peak MONTHS. The obvious idea — in-season (Nov-Apr) closes priced above
off-season closes — does not survive contact with the data. Measured inside
each (ZIP, beds) cell, the high-price tail is the same size in season and out:

    92211 3bd   in-season tail 13.0%   off-season tail 12.8%   lift +0.3%
    92270 3bd   in-season tail 13.6%   off-season tail 11.0%   lift +2.6%

Seasonal leases are booked months ahead of occupancy, so closeDate records
when the deal was struck, not which months it covers. The same test also
rejects the naive version of the rule outright: comparing against a ZIP-wide
(rather than per-bed) baseline flags Beverly Hills 90210 and Malibu 90265 as
the most "seasonal" markets in the dataset, when that tail is simply luxury
price spread.

So: peak-month STRUCTURE stays with the active tiered-profile layer, which
parses real month ranges out of listing remarks. This script supplies the
RATE LEVEL and takes peakMonths from the existing `seasonal` block when one is
present (`peakMonthsSource: "active-tiered-layer"`), falling back to the valley
default of Nov-Apr (`"default-nov-apr"`).

── Known limitation: the threshold truncates its own sample ──────────────────
Selecting on ">= 2x the cell median" means the cohort cannot, by construction,
contain a seasonal lease that closed below 2x. So `seasonalRate` is not "the
typical seasonal rent" — it is the achieved rate among leases priced at a
clearly seasonal level, and it sits high on purpose.

The size of the effect is visible where both layers cover the same ZIP:

    92211   active tiered (asking)   peak $6,000   n=22
            closed leases (achieved) rate $10,000  n=69, 96% furnished-confirmed

Both numbers are real and neither is wrong. They answer different questions —
what the median tiered LISTING asks for its peak months, versus what the
leases that actually transacted at seasonal rates achieved. Read
`seasonalRate` as a peak rate, which is how the annualized fields consume it
(peak months at the seasonal rate, the balance at the long-term baseline).

Lowering TAIL_MULTIPLE widens the cohort but drags in furnished annual
rentals, which is the failure mode this whole selector exists to avoid: at
1.0x the "seasonal" median for 92211 collapses to $3,500 against a $2,800
baseline. 2.0x was chosen because it is where the furnished-field validation
holds (14.8% of furnished vs 0.6% of unfurnished).

── Recency ───────────────────────────────────────────────────────────────────
Checked before assuming it mattered: 92211 3bd long-term closes run $3,000 /
$3,298 / $3,000 / $3,050 / $3,200 / $3,000 for 2021-2026. Flat. So no decay
weighting — it would add opacity for no signal. Instead the output carries
plain medians for the trailing 12 / 24 / 60 months and names which window the
headline came from, so drift stays visible if it ever shows up.

── Output ────────────────────────────────────────────────────────────────────
Written to subdivisions.rentStats.closedSeasonal and rent_rates.closedSeasonal.
Additive: the existing `seasonal` block (active tiered layer) is never touched,
so the two sources can be compared rather than silently merged.

{
  "lastUpdated": ISO, "scope": "subdivision"|"zip", "geo": {...},
  "basis": "closed-furnished" | "closed-tail-inferred" | "insufficient",
  "window": {"months": int, "from": ISO, "to": ISO},
  "sampleSize": int,              # seasonal-cohort closes
  "directCount": int,             # identified by the furnished field
  "inferredCount": int,           # identified by the 2x tail rule
  "longTermSampleSize": int,      # the baseline cohort in the same cell
  "seasonalRate": int|None,       # median ACHIEVED monthly rate among seasonal-priced closes
  "directShare": float|None,      # fraction of the cohort the furnished field confirms
  "longTermBaseline": int|None,   # median achieved monthly rent, long-term cohort
  "seasonalLift": float|None,     # seasonalRate / longTermBaseline
  "peakMonths": [int], "peakMonthsSource": str,
  "annualizedMonthlyBlended": int|None,   # peak at seasonal, rest at long-term
  "annualizedMonthlyPeakOnly": int|None,  # peak at seasonal, rest at ZERO (floor)
  "windows": {"m12": {...}, "m24": {...}, "m60": {...}},
  "byBed": [ {beds,bedsLabel,count,directCount,seasonalRate,longTermBaseline,...} ],
  "quality": {"confidence": "good"|"fair"|"insufficient", "notes": [str]}
}

IMPORTANT — unit discipline, same rule as build-seasonal-rates.py:
`seasonalRate` is an IN-SEASON MONTHLY rate. Only the two `annualizedMonthly*`
fields may feed cash-flow math. Feeding seasonalRate straight into a payment
calculation overstates income roughly 2x.

Usage:
    python3 src/scripts/cma/build-closed-seasonal-rates.py --zip 92211 --dry-run
    python3 src/scripts/cma/build-closed-seasonal-rates.py --slug palm-desert-country-club --dry-run
    python3 src/scripts/cma/build-closed-seasonal-rates.py --city "Palm Desert" --verbose
    python3 src/scripts/cma/build-closed-seasonal-rates.py --all

Suggested cron (twice a week, after build-seasonal-rates.py so peakMonths are
fresh — the data-refresh cadence is Mon + Thu at 1 AM, not daily):
    45 1 * * 1,4 cd /root/jpsrealtor && .venv/bin/python3 src/scripts/cma/build-closed-seasonal-rates.py --all >> /var/log/closed-seasonal-rates.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
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

# ── constants ─────────────────────────────────────────────────────────────────
WINDOW_MONTHS = 60            # seasonal comps are scarce; rent-rates uses 24 for long-term
MIN_SEASONAL = 3              # < this ⇒ insufficient, rates stay None
GOOD_SEASONAL = 8             # >= this ⇒ "good" confidence
MIN_CELL_FOR_TAIL = 15        # a cell needs this many closes before the 2x tail rule is trusted
TAIL_MULTIPLE = 2.0           # "seasonal" inferred at >= this x the cell median
MIN_BASELINE = 3              # long-term comps needed before a baseline is published

# `subdivisionName` carries placeholders where an MLS has no real community
# name. They collide across unrelated listings, so a "subdivision" block built
# from them would be a bucket of strangers. Same exclusion build-pool-premium.py
# needed (P4.2 known limit (b)): prefer ZIP, skip the placeholder.
PLACEHOLDER_SUBDIVISIONS = {"other", "none", "n/a", "na", "unknown", "not in a development",
                            "not applicable", "no subdivision", "-", "--", "."}
MAX_BEDS_BUCKET = 5           # 5 means "5+", matching build-rent-rates.py
BULK_BATCH = 200

# Price sanity. Monthly rent, not annual: 254,795 of 254,809 closed leases carry
# closePrice > 0 with a $3,695 median; 624 sit above $50k and 37 below $300.
# Those tails are data entry (annual totals, $1 placeholders), not rent.
MIN_RENT, MAX_RENT = 300, 60_000

# Junk closeDates are real: years 2040, 2055, 2099, 2100, 2103, 2917, 2997,
# 3018, 4008, 5015 all appear. The window range below excludes them by
# construction, which is also what keeps the query on the closeDate index.
DATE_FLOOR_YEAR = 2010

# Coachella Valley default when no active tiered profile supplies peak months.
DEFAULT_PEAK_MONTHS = [11, 12, 1, 2, 3, 4]

LEASE_MATCH = {"$or": [{"propertyType": "B"}, {"propertyTypeName": "Residential Lease"}]}

LEASE_PROJECTION = {
    "listingKey": 1, "closePrice": 1, "closeDate": 1, "bedsTotal": 1,
    "bedroomsTotal": 1, "city": 1, "postalCode": 1, "subdivisionName": 1,
    "livingArea": 1, "furnished": 1, "poolYN": 1, "mlsSource": 1, "_id": 0,
}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=900000)
db = client.get_database()
subs_coll = db.subdivisions
closed_coll = db.unified_closed_listings
rent_rates_coll = db.rent_rates
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────


def _median_int(values) -> int | None:
    vals = [v for v in values if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    return int(round(statistics.median(vals)))


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


def beds_label(b: int) -> str:
    return f"{b}+" if b >= MAX_BEDS_BUCKET else str(b)


def furnished_class(value) -> str | None:
    """
    'Furnished' / 'Turnkey' ⇒ seasonal cohort. 'Unfurnished' ⇒ long-term.

    The field is free text and multi-valued — real values include
    'Unfurnished, Partially Furnished, Furnished', which is a listing agent
    saying "negotiable". Those are returned as None rather than guessed at.
    """
    if not value:
        return None
    s = str(value).strip().lower()
    has_furnished = bool(re.search(r"(?<!un)furnished", s)) or "turnkey" in s
    has_unfurnished = "unfurnished" in s
    if has_furnished and has_unfurnished:
        return None        # negotiable / both offered — no signal
    if has_furnished:
        return "furnished"
    if has_unfurnished:
        return "unfurnished"
    if "partially" in s:
        return None
    return None


def parse_close_date(value) -> datetime | None:
    """Normalize closeDate to datetime. Defensive: the field is BSON date
    everywhere since ingest/closedate.py, but this also absorbs the str form."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    s = str(value).replace("Z", "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def rent_of(listing: dict) -> float | None:
    p = listing.get("closePrice")
    if isinstance(p, (int, float)) and MIN_RENT <= p <= MAX_RENT:
        return float(p)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# 🏷️ COHORT CLASSIFICATION
# ──────────────────────────────────────────────────────────────────────────────


def classify(leases: list[dict], cell_medians: dict) -> None:
    """
    Tag each lease in place with `_cohort` and `_evidence`.

    _cohort:   "seasonal"        — priced at a peak-season level (the 2x tail)
               "longTerm"        — ordinary rent, not furnished
               "furnishedAnnual" — furnished but ordinarily priced; counted,
                                   but excluded from both medians
               None              — no local threshold to judge it against
    _evidence: "direct"   — the furnished field corroborates
               "inferred" — price alone

    `cell_medians` is keyed (postalCode5, bedsBucket) and is built from the
    whole market, not from the geo being assembled: a subdivision rarely holds
    enough closes to set a trustworthy threshold, and the tail is a property
    of the local market rather than of the individual community.
    """
    for l in leases:
        l["_cohort"] = None
        l["_evidence"] = None
        rent = rent_of(l)
        if rent is None:
            continue

        fc = furnished_class(l.get("furnished"))
        zip5 = str(l.get("postalCode") or "")[:5]
        med = cell_medians.get((zip5, beds_bucket(l)))

        if med is None:
            # No trustworthy local threshold. An unfurnished close is still a
            # usable long-term comp; anything else is left unclassified rather
            # than guessed into a cohort.
            if fc == "unfurnished":
                l["_cohort"], l["_evidence"] = "longTerm", "direct"
            continue

        if rent >= TAIL_MULTIPLE * med:
            l["_cohort"] = "seasonal"
            l["_evidence"] = "direct" if fc == "furnished" else "inferred"
        elif fc == "furnished":
            l["_cohort"], l["_evidence"] = "furnishedAnnual", "direct"
        else:
            l["_cohort"] = "longTerm"
            l["_evidence"] = "direct" if fc == "unfurnished" else "inferred"


def build_cell_medians(leases: list[dict]) -> dict:
    cells = defaultdict(list)
    for l in leases:
        rent = rent_of(l)
        b = beds_bucket(l)
        zip5 = str(l.get("postalCode") or "")[:5]
        if rent is not None and b is not None and zip5:
            cells[(zip5, b)].append(rent)
    return {k: statistics.median(v) for k, v in cells.items() if len(v) >= MIN_CELL_FOR_TAIL}


# ──────────────────────────────────────────────────────────────────────────────
# 🧱 ASSEMBLY
# ──────────────────────────────────────────────────────────────────────────────


def window_block(leases: list[dict], now: datetime, months: int) -> dict:
    start = now - timedelta(days=months * 30)
    seas = [rent_of(l) for l in leases
            if l["_cohort"] == "seasonal" and l["_cd"] >= start and rent_of(l)]
    lt = [rent_of(l) for l in leases
          if l["_cohort"] == "longTerm" and l["_cd"] >= start and rent_of(l)]
    return {"seasonalCount": len(seas), "seasonalRate": _median_int(seas),
            "longTermCount": len(lt), "longTermBaseline": _median_int(lt)}


def by_bed_block(leases: list[dict]) -> list[dict]:
    g = defaultdict(list)
    for l in leases:
        b = beds_bucket(l)
        if b is not None:
            g[b].append(l)
    out = []
    for b in sorted(g):
        cell = g[b]
        seas = [l for l in cell if l["_cohort"] == "seasonal"]
        lt = [l for l in cell if l["_cohort"] == "longTerm"]
        fa = [l for l in cell if l["_cohort"] == "furnishedAnnual"]
        s_rate = _median_int(rent_of(l) for l in seas)
        lt_base = _median_int(rent_of(l) for l in lt) if len(lt) >= MIN_BASELINE else None
        out.append({
            "beds": b,
            "bedsLabel": beds_label(b),
            "count": len(seas),
            "directCount": sum(1 for l in seas if l["_evidence"] == "direct"),
            "furnishedAnnualCount": len(fa),
            "seasonalRate": s_rate if len(seas) >= MIN_SEASONAL else None,
            "longTermCount": len(lt),
            "longTermBaseline": lt_base,
            "seasonalLift": round(s_rate / lt_base, 2)
                            if (s_rate and lt_base and len(seas) >= MIN_SEASONAL) else None,
        })
    return out


def build_quality(n_seasonal: int, n_direct: int, n_lt: int, n_fa: int = 0) -> dict:
    notes: list[str] = []
    if n_seasonal >= GOOD_SEASONAL:
        conf = "good"
    elif n_seasonal >= MIN_SEASONAL:
        conf = "fair"
        notes.append(f"only {n_seasonal} closed seasonal comps — indicative, not a going rate")
    else:
        conf = "insufficient"
        notes.append(f"{n_seasonal} closed seasonal comps, need {MIN_SEASONAL}")

    if n_seasonal:
        inferred = n_seasonal - n_direct
        if n_direct == 0:
            notes.append("no furnished-field evidence — cohort inferred entirely from the 2x tail rule")
        elif inferred:
            notes.append(f"{n_direct} identified by the furnished field, {inferred} by the 2x tail rule")
        else:
            notes.append(f"all {n_direct} identified directly by the furnished field")
    if n_lt < MIN_BASELINE:
        notes.append(f"only {n_lt} long-term comps — annualized figures suppressed without a baseline")
    if n_fa:
        notes.append(f"{n_fa} furnished leases priced at ordinary rents excluded from both medians "
                     f"(furnished-annual, neither a peak rate nor a clean long-term comp)")
    return {"confidence": conf, "notes": notes}


def assemble(scope: str, geo: dict, leases: list[dict], now: datetime,
             peak_months: list[int], peak_source: str) -> dict | None:
    if not leases:
        return None

    seasonal = [l for l in leases if l["_cohort"] == "seasonal"]
    long_term = [l for l in leases if l["_cohort"] == "longTerm"]
    furnished_annual = [l for l in leases if l["_cohort"] == "furnishedAnnual"]
    n_direct = sum(1 for l in seasonal if l["_evidence"] == "direct")
    quality = build_quality(len(seasonal), n_direct, len(long_term), len(furnished_annual))

    base = {
        "lastUpdated": now.isoformat(),
        "scope": scope,
        "geo": geo,
        "window": {"months": WINDOW_MONTHS,
                   "from": (now - timedelta(days=WINDOW_MONTHS * 30)).isoformat(),
                   "to": now.isoformat()},
        "sampleSize": len(seasonal),
        "directCount": n_direct,
        "inferredCount": len(seasonal) - n_direct,
        "longTermSampleSize": len(long_term),
        "furnishedAnnualCount": len(furnished_annual),
        "peakMonths": peak_months,
        "peakMonthsSource": peak_source,
        "quality": quality,
    }

    if quality["confidence"] == "insufficient":
        base.update({"basis": "insufficient", "seasonalRate": None,
                     "longTermBaseline": None, "seasonalLift": None,
                     "annualizedMonthlyBlended": None, "annualizedMonthlyPeakOnly": None,
                     "windows": {}, "byBed": []})
        return base

    s_rate = _median_int(rent_of(l) for l in seasonal)
    lt_base = _median_int(rent_of(l) for l in long_term) if len(long_term) >= MIN_BASELINE else None

    n_peak = len(peak_months)
    blended = peak_only = None
    if s_rate and n_peak:
        # Peak months at the seasonal rate; the remainder either let long-term
        # (blended) or sitting empty (peak-only, the floor an owner can count on).
        peak_only = int(round(s_rate * n_peak / 12))
        if lt_base:
            blended = int(round((s_rate * n_peak + lt_base * (12 - n_peak)) / 12))

    base.update({
        # "closed-furnished" only when the furnished field corroborates most of
        # the cohort — otherwise say plainly that price alone selected it.
        "basis": "closed-furnished" if n_direct >= len(seasonal) / 2 else "closed-tail-inferred",
        "directShare": round(n_direct / len(seasonal), 2) if seasonal else None,
        "seasonalRate": s_rate,
        "longTermBaseline": lt_base,
        "seasonalLift": round(s_rate / lt_base, 2) if (s_rate and lt_base) else None,
        "annualizedMonthlyBlended": blended,
        "annualizedMonthlyPeakOnly": peak_only,
        "windows": {f"m{m}": window_block(leases, now, m) for m in (12, 24, 60)},
        "byBed": by_bed_block(leases),
    })
    return base


# ──────────────────────────────────────────────────────────────────────────────
# 📥 LOAD & GROUP
# ──────────────────────────────────────────────────────────────────────────────


def load_window(scope_match: dict | None, now: datetime) -> list[dict]:
    """
    One indexed scan. Same reasoning as build-rent-rates.py: closeDate is
    indexed and the range both drives the plan and excludes the junk future
    dates; unified_closed_listings has no postalCode index, so per-geo queries
    would collscan the whole 1.1M-doc collection.
    """
    window_start = now - timedelta(days=WINDOW_MONTHS * 30)
    floor = datetime(DATE_FLOOR_YEAR, 1, 1, tzinfo=timezone.utc)
    q = {"closeDate": {"$gte": max(window_start, floor), "$lte": now + timedelta(days=2)},
         **LEASE_MATCH}
    if scope_match:
        q.update(scope_match)
    rows = list(closed_coll.find(q, LEASE_PROJECTION))
    out = []
    for r in rows:
        cd = parse_close_date(r.get("closeDate"))
        if cd is None:
            continue
        r["_cd"] = cd.replace(tzinfo=timezone.utc) if cd.tzinfo is None else cd
        if rent_of(r) is None:
            continue
        out.append(r)
    return out


def group_by_sub(items: list[dict]) -> dict:
    g = defaultdict(list)
    for l in items:
        name = (l.get("subdivisionName") or "").strip()
        if name and name.lower() not in PLACEHOLDER_SUBDIVISIONS:
            g[name.lower()].append(l)
    return g


def group_by_zip(items: list[dict]) -> dict:
    g = defaultdict(list)
    for l in items:
        z = str(l.get("postalCode") or "")[:5]
        if z:
            g[z].append(l)
    return g


def peak_months_for(existing_seasonal: dict | None) -> tuple[list[int], str]:
    """Take real peak months off the active tiered layer when it has them."""
    if existing_seasonal:
        pm = existing_seasonal.get("peakMonths")
        if isinstance(pm, list) and pm:
            return sorted(int(m) for m in pm if isinstance(m, int)), "active-tiered-layer"
    return list(DEFAULT_PEAK_MONTHS), "default-nov-apr"


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Seasonal going rates from closed leases.")
    ap.add_argument("--all", action="store_true", help="Every subdivision and ZIP.")
    ap.add_argument("--slug", help="One subdivision by slug.")
    ap.add_argument("--subdivision", help="One subdivision by name.")
    ap.add_argument("--city", help="Subdivisions in a city, plus its ZIPs.")
    ap.add_argument("--zip", dest="zipcode", help="One ZIP.")
    ap.add_argument("--zips-only", action="store_true")
    ap.add_argument("--subs-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Compute and print, write nothing.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not any([args.all, args.slug, args.subdivision, args.city, args.zipcode]):
        ap.error("pass --all, --slug, --subdivision, --city or --zip")

    # A subdivision-scoped run loads only that subdivision's leases, so any ZIP
    # block built from them would describe a fraction of the ZIP while looking
    # like the whole thing. Writing that over a good block is worse than not
    # running, so the ZIP pass is refused rather than quietly narrowed.
    subdivision_scoped = bool(args.slug or args.subdivision)
    if subdivision_scoped:
        if args.zips_only:
            ap.error("--zips-only cannot be combined with --slug/--subdivision: the lease load "
                     "is restricted to one subdivision, so the ZIP block would be partial")
        args.subs_only = True

    now = datetime.now(timezone.utc)
    t0 = time.time()

    scope_match = None
    if args.city:
        scope_match = {"city": re.compile(f"^{re.escape(args.city)}$", re.I)}
    elif args.zipcode:
        scope_match = {"postalCode": re.compile(f"^{re.escape(args.zipcode)}")}
    elif args.subdivision:
        scope_match = {"subdivisionName": re.compile(f"^{re.escape(args.subdivision)}$", re.I)}
    elif args.slug:
        sub = subs_coll.find_one({"slug": args.slug}, {"name": 1})
        if not sub:
            print(f"[ERROR] no subdivision with slug {args.slug}", file=sys.stderr)
            return 1
        scope_match = {"subdivisionName": re.compile(f"^{re.escape(sub['name'])}$", re.I)}

    print(f"📥 loading closed leases, {WINDOW_MONTHS}-month window …")
    leases = load_window(scope_match, now)
    print(f"   {len(leases):,} closed leases after date + price sanity")
    if not leases:
        print("   nothing to do")
        return 0

    # The tail threshold needs market-wide cells even when the run is scoped to
    # one subdivision, so load those separately rather than from `leases`.
    if scope_match:
        print("   loading market-wide cells for the tail threshold …")
        cell_medians = build_cell_medians(load_window(None, now))
    else:
        cell_medians = build_cell_medians(leases)
    print(f"   {len(cell_medians):,} (zip, beds) cells with >= {MIN_CELL_FOR_TAIL} closes")

    classify(leases, cell_medians)
    n_seas = sum(1 for l in leases if l["_cohort"] == "seasonal")
    n_dir = sum(1 for l in leases if l["_cohort"] == "seasonal" and l["_evidence"] == "direct")
    print(f"   cohorts: {n_seas:,} seasonal ({n_dir:,} direct / {n_seas - n_dir:,} inferred), "
          f"{sum(1 for l in leases if l['_cohort'] == 'longTerm'):,} long-term")

    sub_groups, zip_groups = group_by_sub(leases), group_by_zip(leases)
    sub_ops, zip_ops = [], []
    written = {"subdivisions": 0, "zips": 0, "insufficient": 0}
    skipped_placeholder = 0

    # ── subdivisions ──────────────────────────────────────────────────────────
    if not args.zips_only:
        sub_query = {}
        if args.slug:
            sub_query = {"slug": args.slug}
        elif args.subdivision:
            sub_query = {"name": re.compile(f"^{re.escape(args.subdivision)}$", re.I)}
        elif args.city:
            sub_query = {"city": re.compile(f"^{re.escape(args.city)}$", re.I)}
        for sub in subs_coll.find(sub_query, {"name": 1, "slug": 1, "city": 1,
                                              "dominantPostalCode": 1, "rentStats": 1}):
            name = (sub.get("name") or "").strip()
            if name.lower() in PLACEHOLDER_SUBDIVISIONS:
                skipped_placeholder += 1
                continue
            cell = sub_groups.get(name.lower(), [])
            if not cell:
                continue
            geo = {"subdivision": name, "slug": sub.get("slug"), "city": sub.get("city"),
                   "postalCode": sub.get("dominantPostalCode")}
            pm, src = peak_months_for((sub.get("rentStats") or {}).get("seasonal"))
            block = assemble("subdivision", geo, cell, now, pm, src)
            if not block:
                continue
            if block["quality"]["confidence"] == "insufficient":
                written["insufficient"] += 1
            sub_ops.append(UpdateOne({"_id": sub["_id"]},
                                     {"$set": {"rentStats.closedSeasonal": block}}))
            if args.verbose or args.dry_run:
                print(f"   SUB  {name[:34]:<34} n={block['sampleSize']:>3} "
                      f"rate={block['seasonalRate'] or '-':>6} "
                      f"lt={block['longTermBaseline'] or '-':>6} "
                      f"[{block['quality']['confidence']}]")

    # ── ZIPs ──────────────────────────────────────────────────────────────────
    if not args.subs_only:
        for z, cell in zip_groups.items():
            if args.zipcode and not z.startswith(args.zipcode):
                continue
            existing = rent_rates_coll.find_one({"postalCode": z}, {"seasonal": 1, "geo": 1})
            pm, src = peak_months_for((existing or {}).get("seasonal"))
            city = ((existing or {}).get("geo") or {}).get("city")
            if not city:
                seen = Counter(l.get("city") for l in cell if l.get("city"))
                city = seen.most_common(1)[0][0] if seen else None
            geo = {"postalCode": z, "city": city}
            block = assemble("zip", geo, cell, now, pm, src)
            if not block:
                continue
            if args.city:
                # The load was filtered to one city; a ZIP crossing a city line
                # is only partly represented here. Say so in the document
                # rather than letting a partial block pass as a full one.
                block["partialScope"] = f"city:{args.city}"
                block["quality"]["notes"].append(
                    f"built from a --city {args.city} run — closes in this ZIP outside that "
                    f"city are not included; re-run with --all or --zip for the full block")
            if block["quality"]["confidence"] == "insufficient":
                written["insufficient"] += 1
            zip_ops.append(UpdateOne({"postalCode": z},
                                     {"$set": {"closedSeasonal": block, "scope": "zip"}},
                                     upsert=True))
            if args.verbose or args.dry_run:
                print(f"   ZIP  {z:<10} n={block['sampleSize']:>4} "
                      f"rate={block['seasonalRate'] or '-':>6} "
                      f"lt={block['longTermBaseline'] or '-':>6} "
                      f"[{block['quality']['confidence']}]")

    if args.dry_run:
        print(f"\n[DRY-RUN] would write {len(sub_ops):,} subdivisions + {len(zip_ops):,} ZIPs "
              f"({written['insufficient']:,} insufficient, "
              f"{skipped_placeholder:,} placeholder-named subdivisions skipped)")
        return 0

    for ops, coll, label in ((sub_ops, subs_coll, "subdivisions"), (zip_ops, rent_rates_coll, "zips")):
        for i in range(0, len(ops), BULK_BATCH):
            batch = ops[i:i + BULK_BATCH]
            res = coll.bulk_write(batch, ordered=False)
            written[label] += res.modified_count + (res.upserted_count or 0)

    print(f"\n[DONE] {written['subdivisions']:,} subdivisions, {written['zips']:,} ZIPs "
          f"({written['insufficient']:,} written as insufficient, "
          f"{skipped_placeholder:,} placeholder-named subdivisions skipped) in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
