#!/usr/bin/env python3
"""
Build Rate Profiles

For every Active Residential-Lease (propertyType "B") listing in
unified_listings, extract a structured rent-rate profile from publicRemarks
and persist it as `rateProfile` on the listing doc.

Pipeline mirrors build-listing-cma.py (--workers, --skip-fresh-hours,
--keys-file, --city, --county, --all). Extractor is the regex+Groq stack
validated by audit-rate-tiers.py on 2026-05-04 (8/8 spot-checks correct).

Output `rateProfile` shape (one per listing):
{
    "lastUpdated":       ISO datetime,
    "type":              "flat" | "tiered" | "unknown",
    "source":            "regex" | "llm",
    "confidence":        "high" | "medium" | "low",
    "flatRate":          int | null,                # listPrice when flat, listPrice as informational when unknown
    "tiers":             [{"months":[int],"monthlyRate":int,"label":str}] | null,
    "peakRate":          int | null,                # max across tiers (or flatRate)
    "offPeakRate":       int | null,                # min across tiers (or flatRate)
    "annualizedMonthly": int | null,                # sum(rate × months) / 12 — landlord-perspective avg
    "monthlyMap":        {"1": int, ..., "12": int} | null,
    "signals":           [str]
}

Usage:
    python3 src/scripts/cma/build-rate-profiles.py --key 20260227... --dry-run
    python3 src/scripts/cma/build-rate-profiles.py --city "La Quinta" --workers 4
    python3 src/scripts/cma/build-rate-profiles.py --county Riverside --workers 4
    python3 src/scripts/cma/build-rate-profiles.py --all --workers 4

Suggested cron (twice a week at 1:30 AM, Mon + Thu — half hour after the
listing-CMA cron so we don't double up Atlas connection load):
    30 1 * * 1,4 cd /root/jpsrealtor && .venv/bin/python3 src/scripts/cma/build-rate-profiles.py --all --workers 4 >> /var/log/rate-profiles.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
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
    raise ValueError("Missing MONGODB_URI in .env.local")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# ──────────────────────────────────────────────────────────────────────────────
# 📐 CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

LEASE_PROPERTY_TYPE = "B"
ACTIVE_STATUSES = ["Active", "ComingSoon"]
BULK_BATCH = 50

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB (created lazily per process to play nice with multiprocessing spawn)
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
active_coll = db.unified_listings
print("✅ Connected to MongoDB")

# ──────────────────────────────────────────────────────────────────────────────
# 🧰 REGEX EXTRACTOR (Layer 1) — copied verbatim from audit-rate-tiers.py
# ──────────────────────────────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)
LEASE_TERM_RE = re.compile(
    r"\b(\d{1,2})\s*[- ]?\s*month(?:s)?\s*(?:lease|rental|min(?:imum)?|stay|term|commit)\b",
    re.IGNORECASE,
)
PER_MONTH_HINT = re.compile(r"/\s*mo|per\s*month|/\s*month|monthly|/m\b", re.IGNORECASE)
PER_NIGHT_HINT = re.compile(r"/\s*night|per\s*night|/\s*nt|nightly", re.IGNORECASE)
PER_WEEK_HINT = re.compile(r"/\s*wk|per\s*week|/\s*week|weekly", re.IGNORECASE)
SEASONAL_HINTS = re.compile(
    r"\b(season|seasonal|peak|off[- ]?season|high season|low season|"
    r"shoulder|snowbird|vacation rental|short[- ]?term|holiday)\b",
    re.IGNORECASE,
)


def _normalize_dollar(token: str):
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
    if 500 <= v <= 50000:
        return v
    return None


def _strip_lease_term_phrases(text: str) -> str:
    return LEASE_TERM_RE.sub(" ", text)


def _find_rate_candidates(text: str):
    cleaned = _strip_lease_term_phrases(text)
    out = []
    for m in re.finditer(r"\$\s?\d[\d,.]{2,}|(?<![\d.])\d{4,5}(?![\d.])", cleaned):
        s, e = m.span()
        token = m.group()
        dollars = _normalize_dollar(token)
        if dollars is None:
            continue
        if "$" not in token:
            tail = cleaned[e:e + 30]
            if not PER_MONTH_HINT.search(tail):
                continue
        tail2 = cleaned[e:e + 30]
        if PER_NIGHT_HINT.search(tail2) or PER_WEEK_HINT.search(tail2):
            continue
        out.append((s, e, dollars))
    return out


def _expand_range(months):
    if len(months) != 2:
        return months
    a, b = sorted(months)
    if 1 < b - a <= 6:
        return set(range(a, b + 1))
    if b - a >= 7 and (a + (12 - b)) <= 5:
        return set(range(b, 13)) | set(range(1, a + 1))
    return months


def regex_classify(remarks: str) -> dict:
    """Layer 1: returns {type, tiers, confidence, signals}."""
    if not remarks:
        return {"type": "flat", "tiers": None, "confidence": "high", "signals": ["empty_remarks"]}

    rates = _find_rate_candidates(remarks)
    distinct = sorted({d for _, _, d in rates})
    signals = []
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

    rates_sorted = sorted(rates, key=lambda r: r[0])
    tiers = []
    for idx, (s, e, dollars) in enumerate(rates_sorted):
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

    seen = set()
    merged = []
    for t in tiers:
        key = (tuple(t["months"]), t["monthlyRate"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(t)

    overlap = False
    seen_months = set()
    for t in merged:
        ms = set(t["months"])
        if seen_months & ms:
            overlap = True
            break
        seen_months |= ms

    if len(merged) >= 2 and not overlap:
        return {"type": "tiered", "tiers": merged, "confidence": "medium",
                "signals": signals + ["multi_rate_attached"]}

    note = "multi_rate_no_month_attach" if not merged else "multi_rate_overlapping_months"
    return {"type": "unknown", "tiers": None, "confidence": "low",
            "signals": signals + [note, f"distinct_rates={distinct}"]}


# ──────────────────────────────────────────────────────────────────────────────
# 🤖 GROQ FALLBACK (Layer 2) — copied from audit-rate-tiers.py
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


def _months_to_ints(month_strs):
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


def llm_classify(groq_client, remarks: str) -> dict:
    """Layer 2: call Groq once, return same shape as regex_classify."""
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": GROQ_SYSTEM},
                      {"role": "user", "content": remarks[:4000]}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1500,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
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
        # Hallucination check: each rate should appear in the source text.
        # If none of the LLM's rates can be found in remarks, downgrade.
        rates_in_text = {d for _, _, d in _find_rate_candidates(remarks)}
        llm_rates = {t["monthlyRate"] for t in tiers_out}
        overlap_count = len(rates_in_text & llm_rates)
        if overlap_count == 0 and rates_in_text:
            return {"type": "unknown", "tiers": None, "confidence": "low",
                    "signals": ["llm_hallucinated_rates"]}
        confidence = "medium" if overlap_count >= len(llm_rates) // 2 else "low"
        return {"type": "tiered", "tiers": tiers_out, "confidence": confidence,
                "signals": ["llm_tiered", f"rates_verified={overlap_count}/{len(llm_rates)}"]}
    return {"type": "flat", "tiers": None, "confidence": "low",
            "signals": ["llm_too_few_tiers"]}


# ──────────────────────────────────────────────────────────────────────────────
# 📦 BUILD rateProfile (roll-ups + final shape)
# ──────────────────────────────────────────────────────────────────────────────


def _safe_int_dollars(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and 500 <= v <= 100000:
        return int(v)
    return None


def _build_monthly_map(tiers):
    """Fill 12-month rate map from tier list. Uncovered months get the
    arithmetic mean of off-peak and shoulder, defaulting to off-peak."""
    if not tiers:
        return None
    rates = [t["monthlyRate"] for t in tiers]
    off_peak = min(rates)
    mm = {}
    for t in tiers:
        for m in t["months"]:
            mm[str(m)] = t["monthlyRate"]
    for m in range(1, 13):
        if str(m) not in mm:
            mm[str(m)] = off_peak  # conservative gap-fill
    return mm


def _annualized_monthly(monthly_map):
    if not monthly_map or len(monthly_map) != 12:
        return None
    total = sum(monthly_map[str(m)] for m in range(1, 13))
    return int(round(total / 12))


def build_rate_profile(listing: dict, groq_client) -> dict:
    """
    Run the regex+LLM extractor, compute roll-ups, return the rateProfile
    payload for this listing.
    """
    remarks = (listing.get("publicRemarks") or "").strip()
    list_price_int = _safe_int_dollars(listing.get("listPrice"))

    result = regex_classify(remarks)
    source = "regex"
    if result["type"] == "unknown" and groq_client is not None:
        result = llm_classify(groq_client, remarks)
        source = "llm"

    rtype = result["type"]
    profile: dict = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "type": rtype,
        "source": source,
        "confidence": result["confidence"],
        "flatRate": None,
        "tiers": None,
        "peakRate": None,
        "offPeakRate": None,
        "annualizedMonthly": None,
        "monthlyMap": None,
        "signals": result.get("signals", []),
    }

    if rtype == "flat":
        if list_price_int:
            profile["flatRate"] = list_price_int
            profile["peakRate"] = list_price_int
            profile["offPeakRate"] = list_price_int
            profile["annualizedMonthly"] = list_price_int
            profile["monthlyMap"] = {str(m): list_price_int for m in range(1, 13)}
    elif rtype == "tiered":
        tiers = result.get("tiers") or []
        profile["tiers"] = tiers
        rates = [t["monthlyRate"] for t in tiers]
        if rates:
            profile["peakRate"] = max(rates)
            profile["offPeakRate"] = min(rates)
        profile["monthlyMap"] = _build_monthly_map(tiers)
        profile["annualizedMonthly"] = _annualized_monthly(profile["monthlyMap"])
    else:  # unknown
        # Show listPrice as informational fallback. Roll-ups stay null so
        # consumers know the seasonal structure is uncertain.
        profile["flatRate"] = list_price_int

    return profile


# ──────────────────────────────────────────────────────────────────────────────
# 🛠️ WORKER
# ──────────────────────────────────────────────────────────────────────────────


def _process_listing_chunk(listing_ids: list, options: dict) -> dict:
    """Process a chunk of listing _ids. Spawned per worker."""
    pending: list[UpdateOne] = []
    written = processed = skipped_fresh = failed = 0
    type_counts: Counter = Counter()

    fresh_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=options["skip_fresh_hours"])
        if options["skip_fresh_hours"] > 0 and not options.get("listing_key") else None
    )
    label_prefix = options.get("worker_label", "")
    dry_run = bool(options.get("dry_run"))
    dump_first = bool(options.get("dump_first"))
    use_llm = bool(options.get("use_llm"))

    groq_client = None
    if use_llm and GROQ_API_KEY:
        from openai import OpenAI
        groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

    for _id in listing_ids:
        listing = active_coll.find_one(
            {"_id": _id},
            {"listingKey": 1, "city": 1, "subdivisionName": 1, "listPrice": 1,
             "publicRemarks": 1, "rateProfile.lastUpdated": 1},
        )
        if not listing:
            continue
        if fresh_cutoff is not None:
            existing = listing.get("rateProfile") or {}
            last_updated = existing.get("lastUpdated")
            if isinstance(last_updated, str):
                try:
                    if datetime.fromisoformat(last_updated.replace("Z", "+00:00")) >= fresh_cutoff:
                        skipped_fresh += 1
                        continue
                except ValueError:
                    pass

        processed += 1
        try:
            profile = build_rate_profile(listing, groq_client)
        except Exception as e:
            failed += 1
            print(f"  [ERR{label_prefix}] {listing.get('listingKey')}: {e}", flush=True)
            continue

        type_counts[profile["type"]] += 1
        peak = profile.get("peakRate")
        off = profile.get("offPeakRate")
        ann = profile.get("annualizedMonthly")
        peak_str = f"${peak:,}" if peak else "—"
        off_str = f"${off:,}" if off else "—"
        ann_str = f"${ann:,}/mo annualized" if ann else "—"
        label = f"{listing.get('city','?')} / {(listing.get('subdivisionName') or '—')[:25]}"
        print(
            f"{label_prefix}#{processed}: {label[:38]:<38} "
            f"{profile['type']:7s} ({profile['source']})  "
            f"peak={peak_str:>9s}  off={off_str:>9s}  {ann_str}",
            flush=True,
        )

        if dry_run and dump_first and processed == 1:
            print(json.dumps(profile, indent=2, default=str), flush=True)

        if not dry_run:
            pending.append(UpdateOne({"_id": _id}, {"$set": {"rateProfile": profile}}))
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
        "type_counts": dict(type_counts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────


def _escape_regex(s: str) -> str:
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)


def main():
    parser = argparse.ArgumentParser(description="Pre-compute rateProfile on every active B-type lease listing.")
    parser.add_argument("--all", action="store_true", help="Process every active lease listing.")
    parser.add_argument("--key", "--listing-key", dest="listing_key",
                        help="Process a single listing by listingKey.")
    parser.add_argument("--city", help="Process all active leases in this city.")
    parser.add_argument("--county", help="Process all active leases in this county.")
    parser.add_argument("--keys-file", help="Path to a file with one listingKey per line.")
    parser.add_argument("--limit", type=int, default=0, help="Cap docs processed (0 = no cap).")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write.")
    parser.add_argument("--no-llm", action="store_true", help="Skip the Groq fallback.")
    parser.add_argument("--skip-fresh-hours", type=int, default=60,
                        help="Skip listings whose rateProfile.lastUpdated is younger than this. "
                             "Default 60h fits a twice-weekly schedule. Pass 0 to force a full rebuild.")
    parser.add_argument("--workers", type=int, default=1,
                        help="Run N parallel worker processes. 1 = single-threaded (default). "
                             "4–6 is the sweet spot vs. Atlas/Groq rate limits.")
    args = parser.parse_args()

    if not (args.all or args.listing_key or args.city or args.county or args.keys_file):
        print("[ERROR] Must pass --all, --key, --city, --county, or --keys-file.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    use_llm = not args.no_llm and bool(GROQ_API_KEY)
    if not args.no_llm and not GROQ_API_KEY:
        print("⚠️  GROQ_API_KEY missing; running regex-only.", file=sys.stderr)

    query: dict = {
        "propertyType": LEASE_PROPERTY_TYPE,
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

    fresh_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=args.skip_fresh_hours)
        if args.skip_fresh_hours > 0 and not args.listing_key else None
    )
    if fresh_cutoff is not None:
        query["$or"] = [
            {"rateProfile.lastUpdated": {"$exists": False}},
            {"rateProfile.lastUpdated": {"$lt": fresh_cutoff.isoformat()}},
        ]

    print(f"💰 Counting eligible B-type listings (dry-run={args.dry_run}, "
          f"skip_fresh_hours={args.skip_fresh_hours}, llm={use_llm})…", flush=True)
    id_cursor = active_coll.find(query, {"_id": 1}).batch_size(500)
    if args.limit:
        id_cursor = id_cursor.limit(args.limit)
    listing_ids = [doc["_id"] for doc in id_cursor]

    if not listing_ids:
        print("Nothing to do — no listings matched (or all were fresh).")
        return

    workers = max(1, args.workers)
    print(f"   {len(listing_ids):,} listings to process across {workers} worker(s)", flush=True)

    started = time.time()
    options_base = {
        "skip_fresh_hours": args.skip_fresh_hours,
        "listing_key": args.listing_key,
        "dry_run": args.dry_run,
        "dump_first": args.dry_run,
        "use_llm": use_llm,
    }

    if workers <= 1:
        result = _process_listing_chunk(listing_ids, {**options_base, "worker_label": ""})
        results = [result]
    else:
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
    type_total: Counter = Counter()
    for r in results:
        for k, v in r["type_counts"].items():
            type_total[k] += v

    elapsed = time.time() - started
    print("\n" + "=" * 60)
    print(f"Processed:     {total_processed:,}")
    print(f"Skipped fresh: {total_skipped:,}")
    print(f"Failed:        {total_failed:,}")
    print("Type distribution:")
    for t in ("flat", "tiered", "unknown"):
        if type_total[t]:
            print(f"  {t:>8s}: {type_total[t]:,}")
    print(f"Written:       {total_written:,}" if not args.dry_run else "Dry-run (no writes)")
    print(f"Elapsed:       {elapsed/60:.1f} min ({elapsed:.0f}s)")
    if total_processed:
        print(f"Wall/listing:  {elapsed/total_processed:.1f}s  (workers={workers})")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
