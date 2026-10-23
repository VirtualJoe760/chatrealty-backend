#!/usr/bin/env python3
"""
Rank Subdivisions by Sales Volume (a read-only report over `market_history`)

Answers "which subdivisions are selling the most?" straight out of the annual
archive that build-market-history.py writes. It computes nothing from the raw
feed: every figure printed is a stored field, summed or sorted. That makes it
cheap (26k docs, no scan of 1M closings) and it keeps one definition of a
median in the codebase instead of two.

This is an OPERATOR report, not the read surface. If the app ever needs this
ranking, precompute it into its own collection — do not let a request compute
it. See AGENTS.md.

── The thing that makes this report honest ──────────────────────────────────
`subdivisionName` in this feed is a free-text field, and ~31% of all closings
that carry one carry a PLACEHOLDER: "Other", "Not Applicable", "N/A". Those
are the MLS's way of saying "no subdivision", not the names of enormous
neighborhoods. Ranked naively, they take 8 of the top 10 slots — "Other" in
Corona alone logs 4,033 sales, which would read as the busiest community in
Southern California. They are excluded by default. --include-placeholders
puts them back, marked [placeholder], for when you want the raw picture.

A second, subtler case is kept but flagged: a subdivision whose name is just
its city ("Oceanside" in Oceanside, 4.2% of closings). That is a real area,
only a coarse one, so it stays in the ranking with a `*`.

── Medians across years ─────────────────────────────────────────────────────
Counts sum across years; medians do not. The median of six annual medians is
not a six-year median, so a multi-year run reports the sum of sales and the
median from the LATEST year in range, labelled as such. Nothing here invents
a figure the archive does not hold.

Usage:
    # busiest subdivisions in the latest complete year (the default)
    python3 src/scripts/historical/rank-subdivisions.py

    # one year, top 40
    python3 src/scripts/historical/rank-subdivisions.py --year 2024 --limit 40

    # every archived year, with the per-year breakdown
    python3 src/scripts/historical/rank-subdivisions.py --all --trend

    # one city only
    python3 src/scripts/historical/rank-subdivisions.py --city "Palm Desert"

    # leases instead of sales
    python3 src/scripts/historical/rank-subdivisions.py --type B

    # rank by price or speed of sale rather than volume
    python3 src/scripts/historical/rank-subdivisions.py --sort ppsf --min-count 25

    # hand it to a spreadsheet
    python3 src/scripts/historical/rank-subdivisions.py --all --csv /tmp/subs.csv
"""

import os
import re
import sys
import csv
import json
import argparse
from pathlib import Path
from collections import defaultdict

from pymongo import MongoClient
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
# The union of the placeholder sets already defined in build-listing-cma.py,
# build-search-index.py, build-closed-seasonal-rates.py and
# build-cashflow-profile.py. Kept in sync with them deliberately: the same bad
# names break comps, search, rent rates and this report in the same way.
PLACEHOLDER_SUBDIVISIONS = {
    "not applicable", "n/a", "none", "other", "na", "no hoa", "see remarks",
    "unknown", "tbd", "various", "not in a development", "no subdivision",
    "-", "--", ".", "",
}

# Ventura and Santa Barbara counties spell the same placeholder as an MLS area
# code: "Ojai: Other", "Oxnard: Other - 0031", "Other - 0011". 18 such labels,
# 819 closings. The exact-match set above cannot catch them.
PLACEHOLDER_RE = re.compile(r"^(?:.+:\s*)?other(?:\s*-\s*\d+)?$")

# build-market-history.py refuses to archive an area-year below this, so a
# blank cell in the trend table means "no sales OR too few to store".
MIN_ARCHIVED_SAMPLE = 5

SORT_FIELDS = {
    "count": ("sales", True),
    "price": ("medianClosePrice", True),
    "ppsf": ("medianPricePerSqft", True),
    "dom": ("medianDaysOnMarket", False),   # fastest first
    "ratio": ("medianCloseToListRatio", True),
}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
history_coll = db.market_history
print("✅ Connected to MongoDB")


# ──────────────────────────────────────────────────────────────────────────────
# 🧰 HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def is_placeholder(label):
    """True when this 'subdivision name' means the agent entered nothing."""
    if not isinstance(label, str):
        return True
    norm = label.strip().lower()
    return norm in PLACEHOLDER_SUBDIVISIONS or bool(PLACEHOLDER_RE.match(norm))


def split_key(key):
    """`market_history` subdivision keys are '<city>|<subdivision>', both
    lowercased by build-market-history.py so the name is qualified by its city
    (subdivision names repeat across cities)."""
    city, _, sub = (key or "").partition("|")
    return city.strip(), sub.strip()


def city_named(key):
    """Subdivision name identical to its city — a real area, but a coarse one."""
    city, sub = split_key(key)
    return bool(city) and city == sub


def archived_years(ptype):
    """Every year the archive holds subdivision rows for, with its coverage."""
    rows = history_coll.aggregate([
        {"$match": {"scope": "subdivision", "propertyType": ptype}},
        {"$group": {"_id": "$year",
                    "areas": {"$sum": 1},
                    "sales": {"$sum": "$closedCount"},
                    "partial": {"$max": {"$cond": ["$partialYear", 1, 0]}},
                    "months": {"$max": "$yearMonthsCovered"}}},
        {"$sort": {"_id": 1}},
    ])
    return [{"year": r["_id"], "areas": r["areas"], "sales": r["sales"],
             "partial": bool(r["partial"]), "months": r.get("months")}
            for r in rows]


def name_coverage(ptype, years_wanted):
    """How much of each year a subdivision ranking can actually see.

    Derived from the archive alone: city scope captures every closing that has a
    city, subdivision scope only those that also carry a `subdivisionName`. The
    ratio is therefore how much of the market a subdivision ranking covers.

    This matters because the coverage collapsed. CRMLS stopped publishing
    `SubdivisionName` through Spark around Sept 2025 (the field comes back null,
    and flatten.py drops nulls), so a 2026 ranking rests on roughly a tenth of
    the closings while looking exactly like a 2024 one. Printing the ratio is
    the difference between a thin ranking and a misleading one.
    """
    rows = history_coll.aggregate([
        {"$match": {"propertyType": ptype, "scope": {"$in": ["city", "subdivision"]},
                    "year": {"$in": list(years_wanted)}}},
        {"$group": {"_id": {"y": "$year", "s": "$scope"},
                    "c": {"$sum": "$closedCount"}}},
    ])
    by_year = defaultdict(dict)
    for r in rows:
        by_year[r["_id"]["y"]][r["_id"]["s"]] = r["c"]
    out = {}
    for y, d in by_year.items():
        city, sub = d.get("city", 0), d.get("subdivision", 0)
        out[y] = (sub / city) if city else None
    return out


def default_year(years):
    """The latest COMPLETE year. A partial year is not a fair ranking: with
    MIN_SAMPLE=5 applied to fewer months, small subdivisions drop out of the
    archive entirely, so the list silently becomes a list of big ones."""
    complete = [y for y in years if not y["partial"]]
    return (complete or years)[-1]["year"]


# ──────────────────────────────────────────────────────────────────────────────
# 📊 COLLECT
# ──────────────────────────────────────────────────────────────────────────────

def collect(ptype, year_from, year_to, city, keep_placeholders):
    """One row per subdivision, folded over the years in range."""
    q = {"scope": "subdivision", "propertyType": ptype,
         "year": {"$gte": year_from, "$lte": year_to}}
    rows = defaultdict(lambda: {"sales": 0, "years": {}, "label": None,
                                "placeholder": False, "latest": None})
    skipped = 0
    want = city.strip().lower() if city else None

    for d in history_coll.find(q):
        key = d.get("key")
        ck, _ = split_key(key)
        if want and ck != want:
            continue
        ph = is_placeholder(d.get("label"))
        if ph and not keep_placeholders:
            skipped += d.get("closedCount", 0)
            continue

        r = rows[key]
        r["sales"] += d.get("closedCount", 0)
        r["years"][d["year"]] = d.get("closedCount", 0)
        # The label is a display name chosen per area by dominant vote, so it can
        # differ year to year; the most recent year's is the current one. The
        # placeholder flag has to come from that same year or the printed marker
        # would describe a label that is not the one being shown.
        if r["latest"] is None or d["year"] >= r["latest"]["year"]:
            r["latest"] = d
            r["label"] = d.get("label")
            r["placeholder"] = ph

    out = []
    for key, r in rows.items():
        latest = r["latest"]
        city_k, _ = split_key(key)
        out.append({
            "key": key,
            "label": r["label"],
            "city": city_k,
            "sales": r["sales"],
            "years": r["years"],
            "yearsCovered": len(r["years"]),
            "latestYear": latest["year"],
            "medianClosePrice": latest.get("medianClosePrice"),
            "medianPricePerSqft": latest.get("medianPricePerSqft"),
            "medianDaysOnMarket": latest.get("medianDaysOnMarket"),
            "medianCloseToListRatio": latest.get("medianCloseToListRatio"),
            "confidence": (latest.get("quality") or {}).get("confidence"),
            "placeholder": r["placeholder"],
            "cityNamed": city_named(key),
        })
    return out, skipped


# ──────────────────────────────────────────────────────────────────────────────
# 🖨️ RENDER
# ──────────────────────────────────────────────────────────────────────────────

def _money(v):
    return f"${v:>10,.0f}" if isinstance(v, (int, float)) else " " * 11


def _num(v, w, dp=0):
    return f"{v:>{w},.{dp}f}" if isinstance(v, (int, float)) else " " * w


def render(rows, span, ptype, sort_key, limit, trend, years_in_range):
    multi = span[0] != span[1]
    noun = "leases" if ptype == "B" else "sales"
    price_hdr = f"median {'rent' if ptype == 'B' else 'price'}"

    # In a multi-year run each subdivision's median comes from the latest year
    # IT has rows for, which is not always the latest year in range: a community
    # that stopped selling in 2024 is still ranked by its six-year total. So the
    # year is printed per row rather than promised once in the header.
    yr_col = f"  {'yr':>4}" if multi else ""
    if multi:
        price_hdr += " (latest)"

    hdr = (f"{'#':>3}  {'subdivision':<30} {'city':<18} {noun:>7}{yr_col}  "
           f"{price_hdr:>18}  {'$/sqft':>7}  {'DOM':>5}  {'c/l':>6}  conf")
    print(f"\n{hdr}")
    print("─" * len(hdr))

    for i, r in enumerate(rows[:limit], 1):
        name = (r["label"] or "(unnamed)")[:30]
        if r["cityNamed"]:
            name = name[:28] + " *"
        if r["placeholder"]:
            name = name[:19] + " [placeholder]"
        yr = f"  {r['latestYear']:>4}" if multi else ""
        print(f"{i:>3}  {name:<30} {r['city'][:18]:<18} {r['sales']:>7,}{yr}  "
              f"{_money(r['medianClosePrice']):>18}  "
              f"{_num(r['medianPricePerSqft'], 7, 0)}  "
              f"{_num(r['medianDaysOnMarket'], 5, 0)}  "
              f"{_num(r['medianCloseToListRatio'], 6, 3)}  "
              f"{r['confidence'] or ''}")

    if trend and multi:
        print(f"\nper-year {noun} for the top {min(limit, len(rows))}:")
        cols = "  ".join(f"{y:>6}" for y in years_in_range)
        print(f"  {'subdivision':<30} {'city':<16} {cols}")
        print("  " + "─" * (30 + 16 + len(cols) + 2))
        for r in rows[:limit]:
            cells = "  ".join(f"{r['years'][y]:>6,}" if y in r["years"]
                              else f"{chr(183):>6}" for y in years_in_range)
            print(f"  {(r['label'] or '(unnamed)')[:30]:<30} "
                  f"{r['city'][:16]:<16} {cells}")
        print(f"  {chr(183)} = no archived rows that year (either no sales, or "
              f"fewer than the {MIN_ARCHIVED_SAMPLE} needed to archive an area)")

    legend = []
    if any(r["cityNamed"] for r in rows[:limit]):
        legend.append("*  subdivision name is the same as its city — a real "
                      "area, but coarser than a community")
    if any(r["placeholder"] for r in rows[:limit]):
        legend.append("[placeholder]  not a subdivision: the MLS value means "
                      "'none entered'")
    if legend:
        print()
        for line in legend:
            print(f"  {line}")


def write_csv(rows, path, years_in_range):
    path = Path(path).expanduser()
    cols = ["rank", "subdivision", "city", "sales", "yearsCovered", "latestYear",
            "medianClosePrice", "medianPricePerSqft", "medianDaysOnMarket",
            "medianCloseToListRatio", "confidence", "placeholder", "cityNamed"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"sales_{y}" for y in years_in_range])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r["label"], r["city"], r["sales"], r["yearsCovered"],
                        r["latestYear"], r["medianClosePrice"],
                        r["medianPricePerSqft"], r["medianDaysOnMarket"],
                        r["medianCloseToListRatio"], r["confidence"],
                        r["placeholder"], r["cityNamed"]]
                       + [r["years"].get(y, "") for y in years_in_range])
    print(f"\n📄 wrote {len(rows):,} rows → {path}")


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, help="rank one calendar year")
    ap.add_argument("--since", type=int, metavar="YYYY",
                    help="rank every archived year from YYYY forward")
    ap.add_argument("--all", action="store_true",
                    help="rank every archived year together")
    ap.add_argument("--type", default="A", choices=("A", "B"),
                    help="A = sales (default), B = leases")
    ap.add_argument("--city", help="restrict to one city")
    ap.add_argument("--limit", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--min-count", type=int, default=0,
                    help="drop subdivisions below this many closings")
    ap.add_argument("--sort", default="count", choices=sorted(SORT_FIELDS),
                    help="rank by (default count)")
    ap.add_argument("--include-placeholders", action="store_true",
                    help="keep 'Other'/'Not Applicable' rows, marked")
    ap.add_argument("--trend", action="store_true",
                    help="add a per-year breakdown for the top rows")
    ap.add_argument("--csv", metavar="PATH", help="also write every ranked row to CSV")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--years", action="store_true",
                    help="list what the archive holds, then exit")
    args = ap.parse_args()

    years = archived_years(args.type)
    if not years:
        print(f"\n⚠️  no subdivision rows for property type {args.type} — "
              "run build-market-history.py first")
        return

    if args.years:
        cov = name_coverage(args.type, [y["year"] for y in years])
        print(f"\narchived subdivision-years (type {args.type}):")
        for y in years:
            flag = "" if not y["partial"] else f"   (partial — {y['months']} of 12 months)"
            pct = cov.get(y["year"])
            cstr = f"  names cover {pct*100:>4.0f}%" if pct is not None else ""
            print(f"  {y['year']}  {y['areas']:>5,} subdivisions  "
                  f"{y['sales']:>8,} closings{cstr}{flag}")
        return

    span_all = (years[0]["year"], years[-1]["year"])
    if args.all:
        lo, hi = span_all
    elif args.since:
        lo, hi = args.since, span_all[1]
    elif args.year:
        lo = hi = args.year
    else:
        lo = hi = default_year(years)
        print(f"\nℹ️  no year given — using {lo}, the latest complete archived year")

    in_range = [y for y in years if lo <= y["year"] <= hi]
    if not in_range:
        print(f"\n⚠️  the archive holds nothing for {lo}–{hi} "
              f"(it has {span_all[0]}–{span_all[1]})")
        return
    years_in_range = [y["year"] for y in in_range]

    partial = [y for y in in_range if y["partial"]]
    if partial:
        bits = ", ".join(f"{y['year']} ({y['months']} of 12 months)" for y in partial)
        print(f"\n⚠️  partial year(s) in range: {bits}")
        print("    counts there are not comparable to a full year, and small "
              "subdivisions are missing from them entirely")

    cov = name_coverage(args.type, years_in_range)
    thin = {y: v for y, v in cov.items() if v is not None and v < 0.20}
    if thin:
        print("\n⚠️  subdivision-name coverage is thin in: "
              + ", ".join(f"{y} ({v*100:.0f}%)" for y, v in sorted(thin.items())))
        print("    that is the share of archived closings carrying a "
              "subdivisionName at all. CRMLS")
        print("    stopped publishing it through Spark around Sept 2025, so a "
              "ranking in those")
        print("    years describes the named minority, not the market — prefer "
              "the zip or city scope.")

    rows, skipped = collect(args.type, lo, hi, args.city,
                            args.include_placeholders)
    if args.min_count:
        rows = [r for r in rows if r["sales"] >= args.min_count]
    if not rows:
        print("\n⚠️  nothing matched"
              + (f" for city {args.city!r}" if args.city else ""))
        return

    field, desc = SORT_FIELDS[args.sort]
    # A missing median must not sort as if it were zero (or as the best).
    rows.sort(key=lambda r: (r.get(field) is not None, r.get(field) or 0),
              reverse=desc)
    if not desc:
        rows = [r for r in rows if r.get(field) is not None] + \
               [r for r in rows if r.get(field) is None]

    if args.json:
        print(json.dumps({"propertyType": args.type, "yearFrom": lo, "yearTo": hi,
                          "sort": args.sort, "city": args.city,
                          "placeholdersIncluded": args.include_placeholders,
                          "subdivisions": len(rows), "rows": rows[:args.limit]},
                         indent=2, default=str))
        return

    scope = f"{lo}" if lo == hi else f"{lo}–{hi}"
    head = (f"\nBusiest subdivisions — {scope}, property type {args.type}"
            + (f", {args.city}" if args.city else ""))
    print(head)
    print(f"ranked by {args.sort}, {len(rows):,} subdivisions"
          + (f" with ≥{args.min_count} closings" if args.min_count else ""))

    render(rows, (lo, hi), args.type, args.sort, args.limit, args.trend,
           years_in_range)

    print("\n" + "=" * 60)
    print(f"Subdivisions ranked: {len(rows):,}")
    print(f"Closings covered:    {sum(r['sales'] for r in rows):,}")
    if skipped:
        print(f"Placeholder closings excluded: {skipped:,} "
              "(pass --include-placeholders to see them)")
    print("=" * 60)

    if args.csv:
        write_csv(rows, args.csv, years_in_range)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(130)
