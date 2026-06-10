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
    "loanTermYears": 30,
    "propertyTaxRate": 0.0125,   # CA effective; Riverside ~1.25% (excl. Mello-Roos)
    "insuranceRate": 0.004,      # annual, as fraction of price
    "vacancyPct": 0.05,          # fraction of gross rent
    "managementPct": 0.08,       # fraction of gross rent (assume professionally managed)
    "maintenancePct": 0.05,      # fraction of gross rent (repairs + capex reserve)
    "closingCostPct": 0.03,      # fraction of price, for cash-on-cash denominator
}
DOWN_SCENARIOS = [0.20, 0.25]    # the user's "20% down" + a stricter alternative
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


# ──────────────────────────────────────────────────────────────────────────────
# 💵 CASH-FLOW MATH
# ──────────────────────────────────────────────────────────────────────────────


def compute_cashflow(listing: dict, rent_est: dict, a: dict) -> dict:
    price = _num(listing.get("listPrice")) or 0
    gross_rent = rent_est["monthlyRent"]

    hoa = monthly_hoa(listing)
    tax = round(price * a["propertyTaxRate"] / 12, 2)
    ins = round(price * a["insuranceRate"] / 12, 2)
    vacancy = round(gross_rent * a["vacancyPct"], 2)
    mgmt = round(gross_rent * a["managementPct"], 2)
    maint = round(gross_rent * a["maintenancePct"], 2)

    # Operating expenses excluding debt service (the "below NOI line" is debt).
    opex_monthly = tax + ins + hoa + mgmt + maint + vacancy
    effective_rent = gross_rent - vacancy
    noi_annual = round((effective_rent - (tax + ins + hoa + mgmt + maint)) * 12, 2)
    cap_rate = round(noi_annual / price * 100, 2) if price else None

    fixed_costs = {
        "grossRent": gross_rent, "vacancy": vacancy, "propertyTax": tax,
        "insurance": ins, "hoa": hoa, "management": mgmt, "maintenance": maint,
        "operatingExpensesMonthly": round(opex_monthly, 2),
    }

    scenarios = {}
    for down in DOWN_SCENARIOS:
        loan = price * (1 - down)
        pi = monthly_pi(loan, a["mortgageRate"], a["loanTermYears"])
        monthly_cf = round(effective_rent - (tax + ins + hoa + mgmt + maint) - pi, 2)
        cash_invested = price * down + price * a["closingCostPct"]
        coc = round(monthly_cf * 12 / cash_invested * 100, 2) if cash_invested else None
        dscr = round(noi_annual / (pi * 12), 2) if pi else None
        scenarios[f"down{int(down*100)}"] = {
            "downPct": down,
            "downPayment": round(price * down, 2),
            "loanAmount": round(loan, 2),
            "monthlyPI": pi,
            "monthlyCashflow": monthly_cf,
            "annualCashflow": round(monthly_cf * 12, 2),
            "cashOnCashPct": coc,
            "dscr": dscr,                       # NOI / annual debt service
            "cashflows": monthly_cf > 0,
        }

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "listPrice": price,
        "rentEstimate": rent_est,
        "capRatePct": cap_rate,
        "noiAnnual": noi_annual,
        "grossYieldPct": round(gross_rent * 12 / price * 100, 2) if price else None,
        "fixedCosts": fixed_costs,
        "scenarios": scenarios,
        # `a` already carries mortgageRate, rateSource, and the dynamic note set in main().
        "assumptions": {**a, "downScenarios": DOWN_SCENARIOS,
                        "note": a.get("note", LEASE_NOTE)},
    }


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
    p.add_argument("--rate", type=float, help="Mortgage rate (e.g. 0.0675).")
    p.add_argument("--tax-rate", type=float)
    p.add_argument("--vacancy", type=float)
    p.add_argument("--management", type=float)
    args = p.parse_args()

    if not (args.all or args.city or args.postal or args.listing_key):
        print("[ERROR] Must pass --all, --city, --zip, or --key.", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    a = dict(DEFAULTS)
    # Mortgage rate priority: --rate override > live fetch > fallback (last-stored/default).
    if args.rate is not None:
        a["mortgageRate"] = args.rate
        rate_source = "manual"
        print(f"🏦 mortgage rate: {args.rate*100:.2f}% (manual --rate override)")
    else:
        a["mortgageRate"], rate_source = fetch_current_mortgage_rate(DEFAULTS["mortgageRate"])
    a["rateSource"] = rate_source
    a["note"] = LEASE_NOTE + " " + RATE_NOTE[rate_source]

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

        stats = compute_cashflow(listing, rent_est, a)
        priced += 1
        if stats["scenarios"]["down20"]["cashflows"]:
            cashflowing += 1

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
