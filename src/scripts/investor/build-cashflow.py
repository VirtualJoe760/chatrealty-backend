#!/usr/bin/env python3
"""
Build Cash-Flow Stats (rental investment scoring per sale listing)

Layer 2 of the rental-prediction stack. For every active residential SALE
listing (propertyType "A") in unified_listings, this:

  1. Estimates the monthly market rent the property would command, using the
     going-rate data built by build-rent-rates.py (layer 1):
        • prefer the listing's subdivision rentStats (bedroom-matched), else
        • the ZIP rent_rates doc (bedroom-matched), else
        • the ZIP going rate, else rent-per-sqft × the subject's livingArea.
  2. Runs the investment math (financing + operating expenses) for a couple of
     standard down-payment scenarios and writes a `cashflowStats` block onto the
     listing.

── The three rent strategies under `strategies` ──────────────────────────────
  longTerm        annual lease, from build-rent-rates.py
  seasonal        furnished peak-season ASKING rates, from build-seasonal-rates.py
  closedSeasonal  furnished peak-season ACHIEVED rates, from
                  build-closed-seasonal-rates.py (closed leases)

The two seasonal layers are scored separately and never merged. They answer
different questions — what tiered listings ask for their peak months versus
what seasonal leases actually closed at — and they routinely disagree by a wide
margin (92211: $6,000 asking peak against $10,000 achieved, n=69). Both are
real; collapsing them into one number would throw away the comparison. Neither
feeds the top-level (long-term) figures the existing MCP queries read.

── Why the achieved layer scores more conservatively than asking ─────────────
The two annualized figures are built on different OFF-PEAK assumptions, and
off-peak is 8 of 12 months, so it dominates:

  seasonal.annualizedMonthly        every month at one of the listing's own
                                    furnished asking tiers (92211: off-peak
                                    $3,500)
  closedSeasonal.annualizedMonthlyBlended
                                    peak at the achieved seasonal rate, off-peak
                                    at the achieved LONG-TERM baseline (92211:
                                    $2,800 — unfurnished annual)

So the achieved layer only comes out ahead where the peak lift is large enough
to outrun that off-peak drag. 92211 3bd: achieved $5,400 vs asking $4,704
(peak $10,000 vs $6,000 wins). 92211 4bd: achieved $5,800 vs asking $6,229
(peak ties at $10,000, so the cheaper off-peak decides it). Expect the achieved
strategy to cash-flow on FEWER listings in most areas. That is the layer being
conservative, not broken — nobody has achieved-furnished off-peak rates,
because a furnished off-peak lease is rare enough that inferring one would be
invention.

That makes the MCP / chat query "show me listings in X that cash-flow with 20%
down" a plain Mongo filter+sort:

    unified_listings.find({
        city: "Palm Desert",
        "cashflowStats.scenarios.down20.monthlyCashflow": { $gt: 0 }
    }).sort({ "cashflowStats.scenarios.down20.monthlyCashflow": -1 })

The block also stores the raw rent estimate and fixed monthly costs, so the tool
executor can re-derive cash flow for an arbitrary down-payment / rate WITHOUT a
rebuild (the financing math is trivial arithmetic — only the rent estimate is
expensive, and that's precomputed).

── Assumptions (all overridable via CLI; documented in cashflowStats.assumptions)
There is no taxAnnualAmount in the data, so property tax is derived from price.
Defaults are deliberately conservative; tune per the market.

Usage:
    python3 src/scripts/cma/build-cashflow.py --city "Palm Desert" --dry-run
    python3 src/scripts/cma/build-cashflow.py --all
    python3 src/scripts/cma/build-cashflow.py --all --rate 0.0675 --skip-fresh-hours 60
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

# ── default investment assumptions ───────────────────────────────────────────
# Mid-2026 ballpark for a 30-yr fixed investment-property loan. Not advice — a
# documented, overridable baseline so the numbers are reproducible.
DEFAULTS = {
    "mortgageRate": 0.07,        # annual, 30-yr fixed investment loan
    "investorRateSpread": 0.0075,  # added to frm_30; see load_investor_spread
    "loanTermYears": 30,
    "propertyTaxRate": 0.0125,   # CA effective; Riverside ~1.25% (excl. Mello-Roos)
    "insuranceRate": 0.004,      # annual, as fraction of price
    "vacancyPct": 0.05,          # fraction of gross rent
    "managementPct": 0.08,       # fraction of gross rent (assume professionally managed)
    "maintenancePct": 0.05,      # fraction of gross rent (repairs + capex reserve)
    "closingCostPct": 0.03,      # fraction of price, for cash-on-cash denominator
}
DOWN_SCENARIOS = [0.20, 0.25]    # the user's "20% down" + a stricter alternative

# A furnished seasonal rental is a different operating business from an annual
# lease: it turns over several times a year and is usually professionally managed
# at vacation-rental rates. `annualizedMonthly` already spreads the tiered rates
# across 12 months at FULL occupancy, so the vacancy bump below is what carries
# the risk of unsold weeks.
SEASONAL_OVERRIDES = {
    "managementPct": 0.20,   # vacation-rental management, vs 8% for an annual lease
    "vacancyPct": 0.15,      # unsold shoulder-season weeks
    "maintenancePct": 0.08,  # higher turnover wear + linens/consumables
}
SEASONAL_NOTE = ("Seasonal strategy uses annualizedMonthly (sum(tier rate x months) / 12) "
                 "as gross rent — never the peak rate — with vacation-rental management, "
                 "vacancy and maintenance assumptions.")

# The closed-lease seasonal layer (build-closed-seasonal-rates.py) describes the
# same operating business as the active one — furnished, high turnover, managed
# at vacation-rental rates — so it reuses SEASONAL_OVERRIDES unchanged. Keeping
# the assumptions identical is the point: the two strategies then differ ONLY by
# their rent source (what tiered listings ask vs what seasonal leases closed
# at), which is the comparison this layer exists to make.
CLOSED_SEASONAL_BASES = {"closed-furnished", "closed-tail-inferred"}
CLOSED_SEASONAL_NOTE = (
    "Closed-seasonal strategy uses annualizedMonthlyBlended (peak months at the "
    "ACHIEVED seasonal rate, the balance at the achieved long-term baseline, / 12) "
    "as gross rent — never seasonalRate — with the same vacation-rental "
    "management, vacancy and maintenance assumptions as the seasonal strategy. "
    "These are rates leases actually closed at, not asking rates, and the two "
    "layers routinely disagree; neither supersedes the other.")
BULK_BATCH = 500
MIN_LIST_PRICE = 25000           # below this, the listPrice is junk or a misclassified
                                 # rental — skip so it can't top the cash-flow sort

# ── live mortgage rate (API Ninjas) ──────────────────────────────────────────
# Endpoint returns the weekly Freddie Mac PMMS rates; we want the latest 30-yr
# fixed (`frm_30`, a percent string). Shape observed 2026-06:
#   [{"week":"current","data":{"frm_30":"6.48","frm_15":"5.79","week":"2026-06-04"}}]
API_NINJA_URL = "https://api.api-ninjas.com/v1/mortgagerate"
RATE_MIN, RATE_MAX = 0.02, 0.20   # sane guardrail (2%–20%) — reject garbage

LEASE_NOTE = ("Property tax derived from list price (no taxAnnualAmount in MLS). "
              "Mello-Roos / special assessments not modeled.")

RATE_NOTE = {
    "live": "Mortgage rate is the current 30-yr fixed (Freddie Mac frm_30, live from API Ninjas).",
    "fallback": "Mortgage rate is a fallback (API Ninjas unavailable; last-stored live rate or default).",
    "manual": "Mortgage rate set manually via --rate.",
}

SPREAD_NOTE = ("frm_30 is an owner-occupied rate, so an investor-property spread is "
               "added on top. Every figure here is an estimate: the spread is a "
               "lender-pricing assumption, not a quote, and state and local fees "
               "are not modeled.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
listings_coll = db.unified_listings
subs_coll = db.subdivisions
rent_rates_coll = db.rent_rates
system_config_coll = db.system_config   # persists last live mortgage rate for fallback
print("✅ Connected to MongoDB")

MAX_BEDS_BUCKET = 5

# ──────────────────────────────────────────────────────────────────────────────
# 🏦 LIVE MORTGAGE RATE  (API Ninjas, fetched once per run)
# ──────────────────────────────────────────────────────────────────────────────


def _extract_frm30(payload):
    """
    Pull the latest 30-yr fixed (`frm_30`, a percent) out of API Ninjas' response,
    defensively — the shape can be a list of weekly records, a {data:…} wrapper,
    or a flat object. Prefer a record flagged week=="current".
    """
    records = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            records = payload["data"]
        else:
            records = [payload]
    # current-week record first, then the rest
    records = sorted(records, key=lambda r: 0 if isinstance(r, dict) and r.get("week") == "current" else 1)

    for rec in records:
        if not isinstance(rec, dict):
            continue
        # frm_30 may sit on the record or nested under .data
        for holder in (rec, rec.get("data") if isinstance(rec.get("data"), dict) else None):
            if not isinstance(holder, dict):
                continue
            v = holder.get("frm_30")
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.strip().rstrip("%"))
                except ValueError:
                    pass
    return None


def _fallback_rate(default_rate, reason):
    """Prefer the last successfully-stored LIVE rate; else the hardcoded default."""
    stored = None
    try:
        doc = system_config_coll.find_one({"_id": "mortgage_rate"})
        if doc and RATE_MIN <= doc.get("rate", 0) <= RATE_MAX:
            stored = doc["rate"]
    except Exception:
        pass
    rate = stored if stored is not None else default_rate
    src = "last-stored live" if stored is not None else "hardcoded default"
    print(f"⚠️  mortgage rate: {rate*100:.2f}% (fallback — {reason}; using {src})")
    return rate, "fallback"


def fetch_current_mortgage_rate(default_rate):
    """
    Return (rate_decimal, source) where source ∈ {"live","fallback"}.
    Called ONCE per run. NEVER raises — a rate-fetch failure must not abort the build.
    """
    key = os.getenv("API_NINJA_KEY") or os.getenv("API_NINJAS_KEY")  # accept both spellings
    if not key:
        return _fallback_rate(default_rate, "no API_NINJA_KEY in env")
    try:
        resp = requests.get(API_NINJA_URL, headers={"X-Api-Key": key}, timeout=15)
        if resp.status_code != 200:
            return _fallback_rate(default_rate, f"HTTP {resp.status_code}")
        frm30 = _extract_frm30(resp.json())
        if frm30 is None:
            return _fallback_rate(default_rate, "frm_30 not found in response")
        rate = frm30 / 100.0
        if not (RATE_MIN <= rate <= RATE_MAX):
            return _fallback_rate(default_rate, f"frm_30={frm30} out of sane range")
        try:
            system_config_coll.update_one(
                {"_id": "mortgage_rate"},
                {"$set": {"frm30": frm30, "rate": rate, "source": "live",
                          "fetchedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception:
            pass  # persistence is best-effort; never block the build
        print(f"🏦 mortgage rate: {frm30:.2f}% (live, API Ninjas frm_30)")
        return rate, "live"
    except Exception as e:
        return _fallback_rate(default_rate, f"{type(e).__name__}: {e}")


SPREAD_KEY = "investor_rate_spread"
SPREAD_MIN, SPREAD_MAX = 0.0, 0.05   # 0–5 points; beyond that it is a typo


def load_investor_spread(default_spread, override=None):
    """Return (spread_decimal, source) for the investor-property rate premium.

    `frm_30` is the Freddie Mac owner-occupied 30-yr fixed. An investment-property
    loan prices above it, and by how much is a lender-pricing assumption that
    moves — so it is a stored value, not a constant. Change it without a deploy:

        system_config.updateOne({_id: "investor_rate_spread"},
                                {$set: {spread: 0.01}})

    or `utilities/set-investor-spread.py 1.0`. Precedence is --spread, then the
    stored value, then the default. Never raises: a config problem must not
    abort the build.
    """
    if override is not None:
        print(f"📈 investor spread: +{override*100:.2f}% (manual --spread override)")
        return override, "manual"
    try:
        doc = system_config_coll.find_one({"_id": SPREAD_KEY})
        if doc is not None:
            v = doc.get("spread")
            if isinstance(v, (int, float)) and SPREAD_MIN <= v <= SPREAD_MAX:
                print(f"📈 investor spread: +{v*100:.2f}% (stored, system_config)")
                return float(v), "stored"
            print(f"⚠️  investor spread: stored value {v!r} is not in "
                  f"[{SPREAD_MIN}, {SPREAD_MAX}] — using default")
        else:
            # Seed it so the value is discoverable and editable in the DB.
            system_config_coll.update_one(
                {"_id": SPREAD_KEY},
                {"$set": {"spread": default_spread,
                          "note": "Investor-property rate premium over frm_30. "
                                  "Edit this field to change it; no deploy needed.",
                          "seededAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            print(f"📈 investor spread: +{default_spread*100:.2f}% "
                  f"(default, seeded into system_config)")
            return default_spread, "default-seeded"
    except Exception as e:
        print(f"⚠️  investor spread: {type(e).__name__} reading config — using default")
    print(f"📈 investor spread: +{default_spread*100:.2f}% (default)")
    return default_spread, "default"


# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────


def _num(v):
    return v if isinstance(v, (int, float)) and v > 0 else None


def beds_bucket(beds):
    if not isinstance(beds, (int, float)):
        return None
    return min(int(beds), MAX_BEDS_BUCKET)


def monthly_hoa(listing: dict) -> float:
    """Normalize associationFee to a monthly figure. Frequency is ~45% populated;
    assume Monthly when absent (the dominant case in this data)."""
    fee = _num(listing.get("associationFee"))
    if not fee:
        return 0.0
    freq = (listing.get("associationFeeFrequency") or "").strip().lower()
    if freq.startswith("annual") or freq.startswith("year"):
        return round(fee / 12, 2)
    if freq.startswith("quarter"):
        return round(fee / 3, 2)
    if freq.startswith("semi"):           # semi-annual
        return round(fee / 6, 2)
    if freq.startswith("week"):
        return round(fee * 52 / 12, 2)
    return round(fee, 2)                   # monthly / unknown → treat as monthly


def monthly_pi(principal: float, annual_rate: float, term_years: int) -> float:
    """Standard amortized monthly principal+interest."""
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    n = term_years * 12
    if r == 0:
        return round(principal / n, 2)
    return round(principal * r * (1 + r) ** n / ((1 + r) ** n - 1), 2)


def loan_from_pi(pi: float, annual_rate: float, term_years: int) -> float:
    """Inverse of monthly_pi: the largest loan whose payment is `pi`."""
    if pi <= 0:
        return 0.0
    r = annual_rate / 12
    n = term_years * 12
    if r == 0:
        return pi * n
    return pi * ((1 + r) ** n - 1) / (r * (1 + r) ** n)


def break_even_down(price: float, effective_rent: float, opex_ex_debt: float,
                    a: dict) -> tuple:
    """Smallest down-payment fraction at which monthly cash flow reaches zero.

    Stored so the question "what do I need to put down here" is a field read
    rather than a request-time solve, and so "what cash-flows at 20% down"
    becomes find({breakEvenDownPct: {$lte: 0.20}}).

    Returns (pct, note). pct is None when no down payment works, which happens
    when operating costs alone already exceed effective rent — a 100% cash buyer
    would still lose money monthly.
    """
    pi_budget = effective_rent - opex_ex_debt
    if pi_budget <= 0:
        return None, ("operating costs alone exceed effective rent; negative at any "
                      "down payment including all-cash")
    max_loan = loan_from_pi(pi_budget, a["mortgageRate"], a["loanTermYears"])
    if max_loan >= price:
        return 0.0, "cash-flows with no money down at this rent and rate"
    return round(min(max(1 - max_loan / price, 0.0), 1.0), 4), None


# ──────────────────────────────────────────────────────────────────────────────
# 📊 RENT ESTIMATE  (consumes layer-1 rent_rates + subdivision rentStats)
# ──────────────────────────────────────────────────────────────────────────────


def _bedroom_rent(rent_stats: dict, beds_b):
    """Pull a bedroom-matched median rent from a rentStats block, if available."""
    if beds_b is None:
        return None
    for entry in rent_stats.get("byBedroom", []) or []:
        if entry.get("beds") == beds_b and entry.get("count", 0) >= 3:
            return _num(entry.get("medianRent"))
    return None


def estimate_rent(listing: dict, zip_index: dict, sub_index: dict) -> dict | None:
    """
    Returns {monthlyRent, source, confidence, geo} or None when no rent signal.
    Source precedence: subdivision-bed > subdivision-going > zip-bed > zip-going
    > zip-ppsf.
    """
    beds_b = beds_bucket(listing.get("bedsTotal"))
    sqft = _num(listing.get("livingArea"))

    # 1) Subdivision rentStats (most local) — only ~13% of sale listings carry a
    #    subdivisionName, but when they do and it has signal, prefer it.
    name = (listing.get("subdivisionName") or "").strip().lower()
    city = (listing.get("city") or "").strip().lower()
    sub = sub_index.get((city, name)) if name else None
    if sub and sub.get("quality", {}).get("confidence") in ("good", "fair"):
        r = _bedroom_rent(sub, beds_b)
        if r:
            return {"monthlyRent": r, "source": "subdivision-bedroom",
                    "confidence": "high", "geo": sub.get("geo")}
        gr = _num(sub.get("goingRate", {}).get("monthlyRent"))
        if gr:
            return {"monthlyRent": gr, "source": "subdivision-going-rate",
                    "confidence": "medium", "geo": sub.get("geo")}

    # 2) ZIP rent_rates
    zdoc = zip_index.get(listing.get("postalCode"))
    if zdoc and zdoc.get("quality", {}).get("confidence") in ("good", "fair"):
        r = _bedroom_rent(zdoc, beds_b)
        if r:
            return {"monthlyRent": r, "source": "zip-bedroom",
                    "confidence": "high", "geo": zdoc.get("geo")}
        gr = _num(zdoc.get("goingRate", {}).get("monthlyRent"))
        if gr:
            return {"monthlyRent": gr, "source": "zip-going-rate",
                    "confidence": "medium", "geo": zdoc.get("geo")}
        rpsf = _num(zdoc.get("goingRate", {}).get("rentPerSqft"))
        if rpsf and sqft:
            return {"monthlyRent": round(rpsf * sqft, 2), "source": "zip-rent-per-sqft",
                    "confidence": "low", "geo": zdoc.get("geo")}
    return None




def _seasonal_bed(block: dict, beds_b):
    """Bedroom-matched annualizedMonthly from a `seasonal` block. 2 comps is a
    deliberately low bar — seasonal tiered comps are scarce (~4.75% of profiles)
    — so the sampleSize travels with the estimate for the consumer to weigh."""
    if beds_b is None:
        return None, 0
    for e in block.get("byBed") or []:
        if e.get("beds") == beds_b and e.get("count", 0) >= 2:
            return _num(e.get("annualizedMonthly")), e.get("count", 0)
    return None, 0


def _seasonal_payload(block: dict, rent, source, confidence, sample):
    """NOTE: monthlyRent is annualizedMonthly, never peakRate. peak/offPeak ride
    along for display only; putting a peak rate into a payment calculation
    overstates income by 2x or more."""
    return {
        "monthlyRent": rent,
        "basis": "annualizedMonthly",
        "source": source,
        "confidence": confidence,
        "sampleSize": sample,
        "peakRate": block.get("peakRate"),
        "offPeakRate": block.get("offPeakRate"),
        "peakMonths": block.get("peakMonths") or [],
        "geo": block.get("geo"),
    }


def estimate_seasonal_rent(listing: dict, zip_index: dict, sub_index: dict) -> dict | None:
    """Seasonal gross rent for a sale listing, from the layer-1.5 aggregates
    written by build-seasonal-rates.py. Falls back subdivision -> ZIP -> None and
    records which source it used; never extrapolates from an insufficient block."""
    beds_b = beds_bucket(listing.get("bedsTotal"))
    name = (listing.get("subdivisionName") or "").strip().lower()
    city = (listing.get("city") or "").strip().lower()

    sub = sub_index.get((city, name)) if name else None
    if sub:
        s = sub.get("seasonal") or {}
        if s.get("basis") == "tiered-median":
            r, n = _seasonal_bed(s, beds_b)
            if r:
                return _seasonal_payload(s, r, "subdivision-seasonal-bedroom", "high", n)
            if _num(s.get("annualizedMonthly")):
                return _seasonal_payload(s, _num(s.get("annualizedMonthly")),
                                         "subdivision-seasonal-area", "medium",
                                         s.get("sampleSize", 0))

    zdoc = zip_index.get(listing.get("postalCode"))
    if zdoc:
        s = zdoc.get("seasonal") or {}
        if s.get("basis") == "tiered-median":
            r, n = _seasonal_bed(s, beds_b)
            if r:
                return _seasonal_payload(s, r, "zip-seasonal-bedroom", "medium", n)
            if _num(s.get("annualizedMonthly")):
                return _seasonal_payload(s, _num(s.get("annualizedMonthly")),
                                         "zip-seasonal-area", "low",
                                         s.get("sampleSize", 0))
    return None


def _closed_blended(s_rate, lt_base, n_peak):
    """The same arithmetic build-closed-seasonal-rates.py uses for
    annualizedMonthlyBlended: peak months at the achieved seasonal rate, the
    balance at the achieved long-term baseline. Recomputed here per bedroom
    because `byBed` stores the two component rates, not the blend. Requires a
    baseline for exactly the upstream reason — without one, the off-peak months
    have no achieved rent to stand on."""
    if not (s_rate and lt_base and n_peak):
        return None
    n_peak = min(n_peak, 12)
    return int(round((s_rate * n_peak + lt_base * (12 - n_peak)) / 12))


def _closed_seasonal_bed(block: dict, beds_b):
    """Bedroom-matched blended rate from a `closedSeasonal` block. byBed already
    nulls seasonalRate below MIN_SEASONAL and longTermBaseline below
    MIN_BASELINE, so a cell carrying both has cleared the builder's own
    thresholds — no second gate is applied here."""
    if beds_b is None:
        return None, 0
    n_peak = len(block.get("peakMonths") or [])
    for e in block.get("byBed") or []:
        if e.get("beds") != beds_b:
            continue
        return (_closed_blended(_num(e.get("seasonalRate")),
                                _num(e.get("longTermBaseline")), n_peak),
                e.get("count", 0))
    return None, 0


def _closed_seasonal_payload(block: dict, rent, source, confidence, sample):
    """NOTE: monthlyRent is annualizedMonthlyBlended, never seasonalRate. The
    achieved peak rate rides along for display, as does the peak-only floor
    (off-peak months empty) so a consumer can see both ends of the range.

    `evidence` and `directShare` travel with the estimate because the cohort is
    identified two different ways — the furnished field (direct) or the 2x tail
    rule (inferred) — and that distinction changes how much weight it deserves."""
    return {
        "monthlyRent": rent,
        "basis": "annualizedMonthlyBlended",
        "source": source,
        "confidence": confidence,
        "sampleSize": sample,
        "evidence": block.get("basis"),
        "directShare": block.get("directShare"),
        "seasonalRate": block.get("seasonalRate"),
        "longTermBaseline": block.get("longTermBaseline"),
        "seasonalLift": block.get("seasonalLift"),
        "annualizedMonthlyPeakOnly": block.get("annualizedMonthlyPeakOnly"),
        "peakMonths": block.get("peakMonths") or [],
        "window": block.get("window"),
        "geo": block.get("geo"),
    }


def estimate_closed_seasonal_rent(listing: dict, zip_index: dict,
                                  sub_index: dict) -> dict | None:
    """Achieved seasonal gross rent, from the closed-lease layer written by
    build-closed-seasonal-rates.py. Same subdivision -> ZIP -> None fallback as
    estimate_seasonal_rent, and it never extrapolates from an insufficient block.

    This runs ALONGSIDE the active seasonal strategy, not instead of it. One is
    what tiered listings ask, the other what seasonal leases closed at; they
    routinely disagree (92211: $6,000 asking peak against $10,000 achieved,
    n=69), so merging them would destroy the only signal worth having."""
    beds_b = beds_bucket(listing.get("bedsTotal"))
    name = (listing.get("subdivisionName") or "").strip().lower()
    city = (listing.get("city") or "").strip().lower()

    sub = sub_index.get((city, name)) if name else None
    if sub:
        s = sub.get("closedSeasonal") or {}
        if s.get("basis") in CLOSED_SEASONAL_BASES:
            r, n = _closed_seasonal_bed(s, beds_b)
            if r:
                return _closed_seasonal_payload(
                    s, r, "subdivision-closed-seasonal-bedroom", "high", n)
            if _num(s.get("annualizedMonthlyBlended")):
                return _closed_seasonal_payload(
                    s, _num(s.get("annualizedMonthlyBlended")),
                    "subdivision-closed-seasonal-area", "medium", s.get("sampleSize", 0))

    zdoc = zip_index.get(listing.get("postalCode"))
    if zdoc:
        s = zdoc.get("closedSeasonal") or {}
        if s.get("basis") in CLOSED_SEASONAL_BASES:
            r, n = _closed_seasonal_bed(s, beds_b)
            if r:
                return _closed_seasonal_payload(
                    s, r, "zip-closed-seasonal-bedroom", "medium", n)
            if _num(s.get("annualizedMonthlyBlended")):
                return _closed_seasonal_payload(
                    s, _num(s.get("annualizedMonthlyBlended")),
                    "zip-closed-seasonal-area", "low", s.get("sampleSize", 0))
    return None


# ──────────────────────────────────────────────────────────────────────────────
# 💵 CASH-FLOW MATH
# ──────────────────────────────────────────────────────────────────────────────


def _run_strategy(price: float, gross_rent: float, hoa: float, a: dict) -> dict:
    """One rent figure + one assumption set -> costs, scenarios, breakeven.

    Factored out so the long-term and seasonal strategies run through identical
    math and can only differ by their rent and their assumptions.
    """
    tax = round(price * a["propertyTaxRate"] / 12, 2)
    ins = round(price * a["insuranceRate"] / 12, 2)
    vacancy = round(gross_rent * a["vacancyPct"], 2)
    mgmt = round(gross_rent * a["managementPct"], 2)
    maint = round(gross_rent * a["maintenancePct"], 2)

    opex_ex_debt = tax + ins + hoa + mgmt + maint
    effective_rent = gross_rent - vacancy
    noi_annual = round((effective_rent - opex_ex_debt) * 12, 2)
    cap_rate = round(noi_annual / price * 100, 2) if price else None

    fixed_costs = {
        "grossRent": gross_rent, "vacancy": vacancy, "propertyTax": tax,
        "insurance": ins, "hoa": hoa, "management": mgmt, "maintenance": maint,
        "operatingExpensesMonthly": round(opex_ex_debt + vacancy, 2),
    }

    scenarios = {}
    for down in DOWN_SCENARIOS:
        loan = price * (1 - down)
        pi = monthly_pi(loan, a["mortgageRate"], a["loanTermYears"])
        monthly_cf = round(effective_rent - opex_ex_debt - pi, 2)
        cash_invested = price * down + price * a["closingCostPct"]
        scenarios[f"down{int(down*100)}"] = {
            "downPct": down,
            "downPayment": round(price * down, 2),
            "loanAmount": round(loan, 2),
            "monthlyPI": pi,
            "monthlyCashflow": monthly_cf,
            "annualCashflow": round(monthly_cf * 12, 2),
            "cashOnCashPct": (round(monthly_cf * 12 / cash_invested * 100, 2)
                              if cash_invested else None),
            "dscr": round(noi_annual / (pi * 12), 2) if pi else None,
            "cashflows": monthly_cf > 0,
        }

    be_pct, be_note = break_even_down(price, effective_rent, opex_ex_debt, a)
    return {
        "fixedCosts": fixed_costs,
        "scenarios": scenarios,
        "capRatePct": cap_rate,
        "noiAnnual": noi_annual,
        "grossYieldPct": round(gross_rent * 12 / price * 100, 2) if price else None,
        "breakEvenDownPct": be_pct,
        "breakEvenNote": be_note,
    }


def compute_cashflow(listing: dict, rent_est: dict, a: dict,
                     seasonal_est: dict | None = None,
                     closed_seasonal_est: dict | None = None) -> dict:
    """Long-term result stays at the top level unchanged — the MCP tools already
    query cashflowStats.scenarios.down20 — with breakEvenDownPct added beside it
    and every strategy also grouped under `strategies` for symmetry."""
    price = _num(listing.get("listPrice")) or 0
    hoa = monthly_hoa(listing)

    lt = _run_strategy(price, rent_est["monthlyRent"], hoa, a)

    def _furnished_strategy(est: dict | None, note: str) -> dict | None:
        """Both seasonal layers are the same business on different rent sources,
        so they share one code path and can differ only by that rent."""
        if not (est and est.get("monthlyRent")):
            return None
        sa = {**a, **SEASONAL_OVERRIDES}
        return {
            **_run_strategy(price, est["monthlyRent"], hoa, sa),
            "rentEstimate": est,
            "assumptionOverrides": SEASONAL_OVERRIDES,
            "note": note,
        }

    seasonal_block = _furnished_strategy(seasonal_est, SEASONAL_NOTE)
    closed_seasonal_block = _furnished_strategy(closed_seasonal_est, CLOSED_SEASONAL_NOTE)

    out = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "listPrice": price,
        "rentEstimate": rent_est,
        "capRatePct": lt["capRatePct"],
        "noiAnnual": lt["noiAnnual"],
        "grossYieldPct": lt["grossYieldPct"],
        "fixedCosts": lt["fixedCosts"],
        "scenarios": lt["scenarios"],
        "breakEvenDownPct": lt["breakEvenDownPct"],
        "breakEvenNote": lt["breakEvenNote"],
        "strategies": {
            "longTerm": {**lt, "rentEstimate": rent_est},
            "seasonal": seasonal_block,
            # Achieved rates from closed leases. Additive and deliberately not
            # merged with `seasonal` — see CLOSED_SEASONAL_NOTE.
            "closedSeasonal": closed_seasonal_block,
            # No MLS source exists for nightly short-term rates, so this stays a
            # structural unknown rather than a modelled number.
            "shortTerm": {"available": False,
                          "reason": "no MLS source for nightly short-term rates; "
                                    "verify HOA minimum-lease terms and city STR ordinance"},
        },
        "assumptions": {**a, "downScenarios": DOWN_SCENARIOS,
                        "note": a.get("note", LEASE_NOTE)},
    }
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def load_indexes():
    """Preload layer-1 outputs into memory (small: hundreds of ZIPs, ~1.4k subs)."""
    zip_index = {z["postalCode"]: z for z in rent_rates_coll.find({})}
    sub_index = {}
    for s in subs_coll.find({"rentStats": {"$exists": True}},
                            {"rentStats": 1, "name": 1, "city": 1}):
        rs = s.get("rentStats")
        if rs:
            key = ((s.get("city") or "").strip().lower(), (s.get("name") or "").strip().lower())
            sub_index[key] = rs
    print(f"📥 Loaded {len(zip_index):,} ZIP rent docs, {len(sub_index):,} subdivision rentStats")
    return zip_index, sub_index


def main():
    p = argparse.ArgumentParser(description="Build cashflowStats on active sale listings.")
    p.add_argument("--all", action="store_true")
    p.add_argument("--city")
    p.add_argument("--zip", dest="postal")
    p.add_argument("--key", dest="listing_key", help="One listing by listingKey.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip-fresh-hours", type=int, default=0,
                   help="Skip listings whose cashflowStats is newer than this.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    # assumption overrides
    p.add_argument("--rate", type=float,
                   help="Base mortgage rate before the investor spread (e.g. 0.0675).")
    p.add_argument("--spread", type=float,
                   help="Investor-property rate premium over the base rate "
                        "(e.g. 0.0075). Default is the stored system_config value.")
    p.add_argument("--tax-rate", type=float)
    p.add_argument("--vacancy", type=float)
    p.add_argument("--management", type=float)
    args = p.parse_args()

    if not (args.all or args.city or args.postal or args.listing_key):
        print("[ERROR] Must pass --all, --city, --zip, or --key.", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    a = dict(DEFAULTS)
    # Base rate priority: --rate override > live fetch > fallback (last-stored/default).
    if args.rate is not None:
        base_rate = args.rate
        rate_source = "manual"
        print(f"🏦 base rate: {base_rate*100:.2f}% (manual --rate override)")
    else:
        base_rate, rate_source = fetch_current_mortgage_rate(DEFAULTS["mortgageRate"])

    # frm_30 is owner-occupied; an investment loan prices above it.
    spread, spread_source = load_investor_spread(
        DEFAULTS["investorRateSpread"], override=args.spread)

    effective = base_rate + spread
    if not (RATE_MIN <= effective <= RATE_MAX):
        print(f"⚠️  base {base_rate*100:.2f}% + spread {spread*100:.2f}% = "
              f"{effective*100:.2f}% is outside [{RATE_MIN*100:.0f}%, "
              f"{RATE_MAX*100:.0f}%] — dropping the spread")
        effective, spread, spread_source = base_rate, 0.0, "dropped-out-of-range"

    a["baseRate"] = round(base_rate, 6)
    a["investorRateSpread"] = round(spread, 6)
    a["mortgageRate"] = round(effective, 6)
    a["rateSource"] = rate_source
    a["spreadSource"] = spread_source
    a["note"] = LEASE_NOTE + " " + RATE_NOTE[rate_source] + " " + SPREAD_NOTE
    print(f"🏦 effective investor rate: {effective*100:.2f}% "
          f"(base {base_rate*100:.2f}% + {spread*100:.2f}%)")

    if args.tax_rate is not None:
        a["propertyTaxRate"] = args.tax_rate
    if args.vacancy is not None:
        a["vacancyPct"] = args.vacancy
    if args.management is not None:
        a["managementPct"] = args.management

    query = {"propertyType": "A", "standardStatus": {"$in": ["Active", "ComingSoon"]}}
    if args.city:
        query["city"] = args.city
    if args.postal:
        query["postalCode"] = args.postal
    if args.listing_key:
        query = {"listingKey": args.listing_key}

    zip_index, sub_index = load_indexes()

    cursor = listings_coll.find(query)
    if args.limit:
        cursor = cursor.limit(args.limit)

    started = time.time()
    pending: list[UpdateOne] = []
    processed = priced = no_rent = skipped_fresh = cashflowing = written = bad_price = 0
    seasonal_priced = seasonal_cashflowing = 0
    closed_seasonal_priced = closed_seasonal_cashflowing = 0
    fresh_cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.skip_fresh_hours)
                    if args.skip_fresh_hours else None)

    for listing in cursor:
        processed += 1
        if fresh_cutoff:
            prev = (listing.get("cashflowStats") or {}).get("lastUpdated")
            if prev:
                try:
                    if datetime.fromisoformat(prev) > fresh_cutoff:
                        skipped_fresh += 1
                        continue
                except ValueError:
                    pass

        price = listing.get("listPrice")
        if not isinstance(price, (int, float)) or price < MIN_LIST_PRICE:
            bad_price += 1
            continue

        rent_est = estimate_rent(listing, zip_index, sub_index)
        if not rent_est:
            no_rent += 1
            continue
        seasonal_est = estimate_seasonal_rent(listing, zip_index, sub_index)
        closed_seasonal_est = estimate_closed_seasonal_rent(listing, zip_index, sub_index)

        stats = compute_cashflow(listing, rent_est, a, seasonal_est, closed_seasonal_est)
        priced += 1
        if stats["scenarios"]["down20"]["cashflows"]:
            cashflowing += 1
        seas = stats["strategies"]["seasonal"]
        if seas:
            seasonal_priced += 1
            if seas["scenarios"]["down20"]["cashflows"]:
                seasonal_cashflowing += 1
        cseas = stats["strategies"]["closedSeasonal"]
        if cseas:
            closed_seasonal_priced += 1
            if cseas["scenarios"]["down20"]["cashflows"]:
                closed_seasonal_cashflowing += 1

        if args.verbose or (args.dry_run and priced <= 3):
            d20 = stats["scenarios"]["down20"]
            print(f"  {listing.get('listingKey','?')[:24]} {str(listing.get('city',''))[:16]:<16} "
                  f"${stats['listPrice']:>10,.0f}  rent ${rent_est['monthlyRent']:>6,.0f} "
                  f"({rent_est['source']})  CF20 ${d20['monthlyCashflow']:>7,.0f}  "
                  f"cap {stats['capRatePct']}%  CoC {d20['cashOnCashPct']}%")
        if args.dry_run and priced <= 1:
            print(json.dumps(stats, indent=2, default=str))

        if not args.dry_run:
            pending.append(UpdateOne({"_id": listing["_id"]}, {"$set": {"cashflowStats": stats}}))
            if len(pending) >= BULK_BATCH:
                written += listings_coll.bulk_write(pending, ordered=False).modified_count
                pending.clear()

    if not args.dry_run and pending:
        written += listings_coll.bulk_write(pending, ordered=False).modified_count

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    print(f"Processed:        {processed:,}")
    print(f"Priced:           {priced:,}")
    print(f"  Cash-flowing@20%:{cashflowing:,}")
    print(f"Seasonal priced:  {seasonal_priced:,}")
    print(f"  Cash-flowing@20%:{seasonal_cashflowing:,}  (seasonal strategy, asking)")
    print(f"Closed-seas priced:{closed_seasonal_priced:,}")
    print(f"  Cash-flowing@20%:{closed_seasonal_cashflowing:,}  (closedSeasonal strategy, achieved)")
    print(f"No rent signal:   {no_rent:,}")
    print(f"Bad/low price:    {bad_price:,}")
    if fresh_cutoff:
        print(f"Skipped (fresh):  {skipped_fresh:,}")
    print(f"Written:          {written:,}" if not args.dry_run else "Dry-run (no writes)")
    print(f"Elapsed:          {elapsed/60:.1f} min")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
