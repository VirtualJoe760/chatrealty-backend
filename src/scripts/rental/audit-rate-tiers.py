#!/usr/bin/env python3
"""
Audit Rate-Tier Extraction (read-only)

Scans B-type (Residential Lease) listings and tries to classify each one as:
    flat        — single rent rate (or none extractable)
    tiered      — 2+ distinct rate amounts attached to specific months
    unknown     — multi-rate text the regex couldn't cleanly parse
                  (these get a Groq fallback when --use-llm is on)

Outputs a CSV at /root/jpsrealtor/local-logs/rate-tier-audit.csv with one row
per listing so we can hand-eval accuracy before promoting this logic into a
production builder.

NO DB WRITES. Read-only against unified_listings.

Usage:
    python3 src/scripts/cma/audit-rate-tiers.py                 # CV cities, all of them
    python3 src/scripts/cma/audit-rate-tiers.py --no-llm        # regex only
    python3 src/scripts/cma/audit-rate-tiers.py --limit 100     # cap sample
    python3 src/scripts/cma/audit-rate-tiers.py --city "Palm Springs"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

if not MONGO_URI:
    raise ValueError("Missing MONGODB_URI in .env.local")

COACHELLA_VALLEY = [
    "Palm Springs", "Palm Desert", "La Quinta", "Indian Wells",
    "Rancho Mirage", "Cathedral City", "Indio", "Desert Hot Springs",
    "Coachella", "Thermal", "Bermuda Dunes", "Mecca", "Thousand Palms",
    "Sky Valley",
]

OUTPUT_PATH = Path("/root/jpsrealtor/local-logs/rate-tier-audit.csv")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 LAYER 1 — REGEX CLASSIFIER
# ──────────────────────────────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)
# Lease-term phrases that contain a number — false positives for rate detection.
LEASE_TERM_RE = re.compile(
    r"\b(\d{1,2})\s*[- ]?\s*month(?:s)?\s*(?:lease|rental|min(?:imum)?|stay|term|commit)\b",
    re.IGNORECASE,
)
# A reasonable monthly-rent dollar pattern: requires `$` OR a /mo|month suffix nearby.
DOLLAR_RE = re.compile(
    r"""
    (?:                                       # capture group 1: the amount
        \$\s?(\d{1,2}(?:,\d{3})|\d{3,5})(?:\.\d{0,2})?  # $X,XXX or $XXXX
        |
        (?<![\d.])(\d{1,2}(?:,\d{3})|\d{3,5})(?:\.\d{0,2})?  # bare 4-5 digit
    )
    \s*
    (?:k\b)?                                 # optional 'k' suffix
    """,
    re.IGNORECASE | re.VERBOSE,
)
PER_MONTH_HINT = re.compile(r"/\s*mo|per\s*month|/\s*month|monthly|/m\b", re.IGNORECASE)
PER_NIGHT_HINT = re.compile(r"/\s*night|per\s*night|/\s*nt|nightly", re.IGNORECASE)
PER_WEEK_HINT = re.compile(r"/\s*wk|per\s*week|/\s*week|weekly", re.IGNORECASE)
SEASONAL_HINTS = re.compile(
    r"\b(season|seasonal|peak|off[- ]?season|high season|low season|"
    r"shoulder|snowbird|vacation rental|short[- ]?term|holiday)\b",
    re.IGNORECASE,
)


def _normalize_dollar(token: str) -> Optional[int]:
    """Convert '$8,000' / '8000' / '6k' → int dollars. Returns None for noise."""
    s = token.replace(",", "").replace("$", "").strip().lower()
    k = s.endswith("k")
    if k:
        s = s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    if k:
        v *= 1000
    v = int(v)
    # Realistic monthly-rent guardrails — $500/mo to $50K/mo.
    if 500 <= v <= 50000:
        return v
    return None


def _strip_lease_term_phrases(text: str) -> str:
    """Remove '12 month lease' style phrases so they don't pollute rate extraction."""
    return LEASE_TERM_RE.sub(" ", text)


def _find_rate_candidates(text: str) -> list[tuple[int, int, int]]:
    """
    Return [(start, end, dollars)] for every plausible monthly-rent dollar amount
    in the text. We require either a `$` or a /mo|month suffix within a small
    window for unambiguous rate detection.
    """
    cleaned = _strip_lease_term_phrases(text)
    out: list[tuple[int, int, int]] = []
    for m in re.finditer(r"\$\s?\d[\d,.]{2,}|(?<![\d.])\d{4,5}(?![\d.])", cleaned):
        s, e = m.span()
        token = m.group()
        dollars = _normalize_dollar(token)
        if dollars is None:
            continue
        # If it's not $-prefixed, require a /mo-style hint within next 30 chars
        if "$" not in token:
            tail = cleaned[e:e + 30]
            if not PER_MONTH_HINT.search(tail):
                continue
        # Skip if it's actually $X/night or $X/week
        tail2 = cleaned[e:e + 30]
        if PER_NIGHT_HINT.search(tail2) or PER_WEEK_HINT.search(tail2):
            continue
        out.append((s, e, dollars))
    return out


def _months_near(text: str, start: int, end: int, window: int = 80) -> set[int]:
    """Find month tokens within `window` chars before/after the rate location."""
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    found = set()
    for m in MONTH_RE.finditer(text[lo:hi]):
        idx = MONTHS.get(m.group().lower())
        if idx:
            found.add(idx)
    return found


def _expand_range(months: set[int]) -> set[int]:
    """If we find exactly two months and they look like a range (e.g. {1, 3})
    plus the text says 'Jan-Mar' nearby, fill in the gap. Conservative — we only
    expand when months are distinct and within 5 of each other."""
    if len(months) != 2:
        return months
    a, b = sorted(months)
    if 1 < b - a <= 6:
        return set(range(a, b + 1))
    # Year wraparound: e.g. {11, 2} → Nov-Dec-Jan-Feb
    if b - a >= 7 and (a + (12 - b)) <= 5:
        return set(range(b, 13)) | set(range(1, a + 1))
    return months


def regex_classify(remarks: str) -> dict:
    """
    Returns:
        {
          "type": "flat" | "tiered" | "unknown",
          "tiers": list[tier] | None,
          "confidence": "high" | "medium" | "low",
          "signals": list[str]
        }
    """
    if not remarks:
        return {"type": "flat", "tiers": None, "confidence": "high", "signals": ["empty_remarks"]}

    rates = _find_rate_candidates(remarks)
    distinct = sorted({d for _, _, d in rates})

    signals: list[str] = []
    if SEASONAL_HINTS.search(remarks):
        signals.append("seasonal_keyword")
    if PER_NIGHT_HINT.search(remarks) or PER_WEEK_HINT.search(remarks):
        signals.append("non_monthly_rates_present")

    if len(distinct) == 0:
        return {"type": "flat", "tiers": None, "confidence": "medium",
                "signals": signals + ["no_dollar_amounts"]}
    if len(distinct) == 1:
        return {"type": "flat", "tiers": None, "confidence": "high",
                "signals": signals + ["single_rate"]}

    # Multi-rate — split text into rate-bounded clauses so each rate only
    # absorbs the months that immediately follow it (until the next rate).
    rates_sorted = sorted(rates, key=lambda r: r[0])
    tiers: list[dict] = []
    for idx, (s, e, dollars) in enumerate(rates_sorted):
        # Clause: from a small window before this rate (catches "Oct-Feb $4500")
        # to just before the next rate.
        clause_start = max(0, s - 30)
        clause_end = rates_sorted[idx + 1][0] if idx + 1 < len(rates_sorted) else len(remarks)
        clause = remarks[clause_start:clause_end]
        months = set()
        for m in MONTH_RE.finditer(clause):
            mi = MONTHS.get(m.group().lower())
            if mi:
                months.add(mi)
        months = _expand_range(months)
        if months:
            tiers.append({"months": sorted(months), "monthlyRate": dollars, "label": None})

    # Coalesce duplicates
    seen = set()
    merged: list[dict] = []
    for t in tiers:
        key = (tuple(t["months"]), t["monthlyRate"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(t)

    # Sanity check: do tiers have overlapping months? If yes the attachment
    # heuristic probably got confused — defer to LLM.
    overlap = False
    seen_months: set[int] = set()
    for t in merged:
        ms = set(t["months"])
        if seen_months & ms:
            overlap = True
            break
        seen_months |= ms

    if len(merged) >= 2 and not overlap:
        return {"type": "tiered", "tiers": merged, "confidence": "medium",
                "signals": signals + ["multi_rate_attached"]}

    # Multi-rate but messy attachment (no months, single tier, or overlapping months).
    note = "multi_rate_no_month_attach" if not merged else "multi_rate_overlapping_months"
    return {"type": "unknown", "tiers": None, "confidence": "low",
            "signals": signals + [note, f"distinct_rates={distinct}"]}


# ──────────────────────────────────────────────────────────────────────────────
# 🤖 LAYER 2 — GROQ FALLBACK
# ──────────────────────────────────────────────────────────────────────────────

GROQ_SYSTEM = """You extract seasonal rental-rate tiers from real-estate listing public remarks.

Return ONLY this JSON shape:
{"tiers": [{"months": <array of month-name strings>, "monthlyRate": <integer dollars>, "label": <short string>}]}

If the remarks describe a single flat rate (or no monthly pricing at all), return {"tiers": null}.

Rules:
- months is an array of three-letter month abbreviations: "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec".
- monthlyRate is the integer dollar amount per month — no commas, no dollar sign.
- Skip weekly or nightly rates unless conversion is obvious.
- Only return tiers when 2+ distinct rate amounts attach to specific months.

Examples:

INPUT: Beautiful home, Jan-Mar $8000/mo, Apr-May $5500/mo, summer $3500/mo, Nov-Dec $5500/mo.
OUTPUT: {"tiers":[{"months":["Jan","Feb","Mar"],"monthlyRate":8000,"label":"Jan-Mar"},{"months":["Apr","May"],"monthlyRate":5500,"label":"Apr-May"},{"months":["Jun","Jul","Aug"],"monthlyRate":3500,"label":"summer"},{"months":["Nov","Dec"],"monthlyRate":5500,"label":"Nov-Dec"}]}

INPUT: Long-term unfurnished rental, $3200/mo, 12 month lease.
OUTPUT: {"tiers":null}

INPUT: Turnkey furnished. Available now $5000/mo.
OUTPUT: {"tiers":null}"""


_MONTH_STR_TO_INT = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _months_to_ints(month_strs) -> list[int]:
    if not isinstance(month_strs, list):
        return []
    out = []
    for m in month_strs:
        if not isinstance(m, str):
            continue
        i = _MONTH_STR_TO_INT.get(m.strip().lower()[:3])
        if i:
            out.append(i)
    return sorted(set(out))


def llm_classify(client, remarks: str) -> dict:
    """Call Groq once. Returns same shape as regex_classify."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": GROQ_SYSTEM},
                      {"role": "user", "content": remarks[:4000]}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw)
    except Exception as e:
        return {"type": "unknown", "tiers": None, "confidence": "low",
                "signals": [f"llm_error:{type(e).__name__}"]}

    tiers_in = data.get("tiers")
    if tiers_in is None:
        return {"type": "flat", "tiers": None, "confidence": "medium",
                "signals": ["llm_no_tiers"]}

    tiers_out = []
    for t in tiers_in:
        months = _months_to_ints(t.get("months", []))
        rate = t.get("monthlyRate")
        if isinstance(rate, str):
            try:
                rate = int(rate.replace(",", "").replace("$", ""))
            except ValueError:
                rate = None
        if not (isinstance(rate, int) and 500 <= rate <= 50000):
            continue
        if not months:
            continue
        tiers_out.append({"months": months, "monthlyRate": rate, "label": t.get("label")})

    if len(tiers_out) >= 2:
        return {"type": "tiered", "tiers": tiers_out, "confidence": "medium",
                "signals": ["llm_tiered"]}
    return {"type": "flat", "tiers": None, "confidence": "low",
            "signals": ["llm_too_few_tiers"]}


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Audit rate-tier extraction on B-type listings.")
    parser.add_argument("--city", action="append", help="Filter to one or more cities (repeatable).")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--no-llm", action="store_true", help="Skip the Groq fallback.")
    parser.add_argument("--out", default=str(OUTPUT_PATH), help="CSV output path.")
    args = parser.parse_args()

    cities = args.city if args.city else COACHELLA_VALLEY

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client.get_database()
    coll = db.unified_listings

    query = {
        "propertyType": "B",
        "city": {"$in": cities},
        "publicRemarks": {"$exists": True, "$ne": ""},
    }

    cursor = coll.find(query, {
        "listingKey": 1, "city": 1, "subdivisionName": 1, "listPrice": 1,
        "publicRemarks": 1, "furnished": 1, "existingLeaseType": 1, "_id": 0,
    })
    if args.limit:
        cursor = cursor.limit(args.limit)
    listings = list(cursor)
    print(f"📋 {len(listings):,} B-type listings to audit "
          f"({'cities=' + ','.join(cities) if args.city else 'Coachella Valley'})")

    # Lazy-init Groq client only if we'll need it
    groq_client = None
    if not args.no_llm:
        if not GROQ_API_KEY:
            print("⚠️  GROQ_API_KEY missing; running regex-only.")
            args.no_llm = True
        else:
            from openai import OpenAI
            groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)

    # Counters
    started = time.time()
    counts = {"flat": 0, "tiered": 0, "unknown": 0}
    layer_counts = {"regex": 0, "llm": 0}
    llm_calls = 0

    fieldnames = [
        "listingKey", "city", "subdivisionName", "listPrice",
        "furnished", "existingLeaseType",
        "type", "source", "confidence",
        "peakRate", "offPeakRate", "tierCount",
        "tiers", "signals", "remarks_excerpt",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        for i, doc in enumerate(listings, 1):
            remarks = (doc.get("publicRemarks") or "").strip()
            result = regex_classify(remarks)
            source = "regex"

            if result["type"] == "unknown" and not args.no_llm and groq_client is not None:
                llm_calls += 1
                llm_result = llm_classify(groq_client, remarks)
                # Promote LLM result regardless — it had more context
                result = llm_result
                source = "llm"

            counts[result["type"]] = counts.get(result["type"], 0) + 1
            layer_counts[source] += 1

            # Roll-ups
            tiers = result.get("tiers") or []
            rates = [t["monthlyRate"] for t in tiers]
            peak = max(rates) if rates else None
            off_peak = min(rates) if rates else None

            w.writerow({
                "listingKey": doc.get("listingKey"),
                "city": doc.get("city"),
                "subdivisionName": doc.get("subdivisionName"),
                "listPrice": doc.get("listPrice"),
                "furnished": doc.get("furnished"),
                "existingLeaseType": doc.get("existingLeaseType"),
                "type": result["type"],
                "source": source,
                "confidence": result["confidence"],
                "peakRate": peak,
                "offPeakRate": off_peak,
                "tierCount": len(tiers),
                "tiers": json.dumps(tiers) if tiers else "",
                "signals": "|".join(result.get("signals", [])),
                "remarks_excerpt": remarks[:240].replace("\n", " "),
            })

            if i % 100 == 0:
                print(f"  {i:>5}/{len(listings)}  flat={counts['flat']}  "
                      f"tiered={counts['tiered']}  unknown={counts['unknown']}  "
                      f"llm_calls={llm_calls}", flush=True)

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    print(f"Audited:    {len(listings):,}")
    print(f"  flat:     {counts['flat']:,}")
    print(f"  tiered:   {counts['tiered']:,}")
    print(f"  unknown:  {counts['unknown']:,}")
    print(f"By source — regex: {layer_counts['regex']:,}   llm: {layer_counts['llm']:,}")
    print(f"LLM calls:  {llm_calls:,}")
    print(f"Elapsed:    {elapsed/60:.1f} min ({elapsed:.0f}s)")
    print(f"CSV:        {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
