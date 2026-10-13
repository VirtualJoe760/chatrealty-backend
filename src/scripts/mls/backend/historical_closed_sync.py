#!/usr/bin/env python3
"""
Historical Closed Sync (daily)

Thin orchestrator that runs the unified closed-listings pipeline for a rolling
recent window each day. The big one-time 5-year backfill is
`unified/closed/fetch.py` (run manually via its own docs); this script is the
daily incremental that tops up `unified_closed_listings` with closings booked
or modified in the last N days. That way the CMA builder always has fresh
comps.

Pipeline per MLS (mirrors unified/run-pipeline.py shape):
    1. fetch  → unified/closed/fetch.py  --mls <X> --years <window>
    2. flatten → unified/closed/flatten.py  --input <raw> --output <flat>
    3. seed    → unified/closed/seed.py     --input <flat>

Crontab (0 10 * * *) schedules this after the 6am/7am pipeline + status jobs.

Usage:
    # Full daily run — last 30 days of closings across all MLSs
    python3 src/scripts/mls/backend/historical_closed_sync.py

    # Narrower window
    python3 src/scripts/mls/backend/historical_closed_sync.py --window-days 7

    # One MLS only
    python3 src/scripts/mls/backend/historical_closed_sync.py --mls GPS

    # Skip a slow MLS
    python3 src/scripts/mls/backend/historical_closed_sync.py --exclude CRMLS
"""

import sys
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MLS_OPTIONS = [
    "GPS", "CRMLS", "CLAW", "SOUTHLAND",
    "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK", "ITECH",
]

DEFAULT_WINDOW_DAYS = 30

# Converts --window-days into the --years argument the underlying fetch script
# expects. unified/closed/fetch.py uses fractional years fine because it
# applies CloseDate ge (now - timedelta(days=years*365.25)).
def _window_days_to_years(days: int) -> float:
    return round(days / 365.25, 4)

# Where the underlying scripts live — one dir down from this file
SCRIPT_DIR = Path(__file__).resolve().parent
UNIFIED_CLOSED_DIR = SCRIPT_DIR / "unified" / "closed"

FETCH_PATH = UNIFIED_CLOSED_DIR / "fetch.py"
FLATTEN_PATH = UNIFIED_CLOSED_DIR / "flatten.py"
SEED_PATH = UNIFIED_CLOSED_DIR / "seed.py"

for p in (FETCH_PATH, FLATTEN_PATH, SEED_PATH):
    if not p.exists():
        print(f"❌ Required script missing: {p}", file=sys.stderr)
        sys.exit(2)


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 STAGE RUNNER
# ──────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], label: str) -> bool:
    print(f"\n{'─' * 70}")
    print(f"▶ {label}")
    print(f"  {' '.join(cmd)}")
    print("─" * 70)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"❌ {label} failed (exit {result.returncode})")
        return False
    print(f"✅ {label} ok")
    return True


# fetch.py writes LOCAL_LOGS_DIR / f"closed_{years}y_{MLS}_listings.json".
# We reconstruct those paths and pass them through explicitly instead of letting
# each stage auto-detect "the most recent file" — with 8 MLSs running in sequence
# that heuristic is fragile, and it is what silently broke this pipeline: flatten
# globbed a hardcoded closed_5y_* while fetch was writing closed_0.0821y_*, so
# all 8 MLSs failed on every run for 146 consecutive runs.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
CLOSED_DIR = PROJECT_ROOT / "local-logs" / "closed"


def _fetch_output(mls: str, years: float) -> Path:
    return CLOSED_DIR / f"closed_{years}y_{mls}_listings.json"


def _flatten_output(mls: str) -> Path:
    return CLOSED_DIR / f"flattened_closed_{mls}_listings.json"


def sync_one_mls(mls: str, years: float) -> bool:
    raw = _fetch_output(mls, years)
    flat = _flatten_output(mls)

    # fetch
    cmd = [sys.executable, str(FETCH_PATH), "--mls", mls, "--years", str(years), "-y"]
    if not _run(cmd, f"[{mls}] fetch (last {years}y)"):
        return False
    if not raw.exists():
        print(f"❌ [{mls}] fetch reported success but {raw.name} is missing — "
              f"skipping flatten/seed so a stale file cannot be re-seeded")
        return False

    # flatten — explicit in and out
    cmd = [sys.executable, str(FLATTEN_PATH), "--input", str(raw), "--output", str(flat)]
    if not _run(cmd, f"[{mls}] flatten"):
        return False
    if not flat.exists():
        print(f"❌ [{mls}] flatten reported success but {flat.name} is missing")
        return False

    # seed — explicit in
    cmd = [sys.executable, str(SEED_PATH), "--input", str(flat)]
    if not _run(cmd, f"[{mls}] seed"):
        return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily rolling closed-listings sync into unified_closed_listings.")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"How far back to look (default {DEFAULT_WINDOW_DAYS} days).")
    parser.add_argument("--mls", nargs="+", choices=MLS_OPTIONS,
                        help="Restrict to specific MLS(s). Default: all.")
    parser.add_argument("--exclude", nargs="+", choices=MLS_OPTIONS,
                        help="Skip specific MLS(s).")
    args = parser.parse_args()

    years = _window_days_to_years(args.window_days)

    mls_list = args.mls or MLS_OPTIONS
    if args.exclude:
        mls_list = [m for m in mls_list if m not in set(args.exclude)]
    if not mls_list:
        print("[ERROR] No MLSs left to process after --exclude filtering.", file=sys.stderr)
        sys.exit(1)

    start = datetime.now()
    print("=" * 70)
    print("Historical Closed Sync")
    print("=" * 70)
    print(f"Window:  last {args.window_days} days  (≈{years} years, as Spark expects)")
    print(f"MLSs:    {', '.join(mls_list)}")
    print(f"Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    ok = 0
    failed: list[str] = []

    for mls in mls_list:
        print(f"\n\n{'#' * 70}\n# {mls}\n{'#' * 70}")
        if sync_one_mls(mls, years):
            ok += 1
        else:
            failed.append(mls)
            print(f"\n[WARN] {mls} failed — continuing with next MLS.")

    end = datetime.now()
    print("\n" + "=" * 70)
    print("HISTORICAL CLOSED SYNC — COMPLETE")
    print("=" * 70)
    print(f"Successful: {ok}/{len(mls_list)}")
    if failed:
        print(f"Failed:     {', '.join(failed)}")
    print(f"Duration:   {end - start}")
    print("=" * 70)

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")
        sys.exit(1)
