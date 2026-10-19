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
    python3 src/scripts/historical/sync.py

    # Narrower window
    python3 src/scripts/historical/sync.py --window-days 7

    # One MLS only
    python3 src/scripts/historical/sync.py --mls GPS

    # Skip a slow MLS
    python3 src/scripts/historical/sync.py --exclude CRMLS
"""

import sys
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 CONFIG
# ──────────────────────────────────────────────────────────────────────────────

# ITECH is deliberately EXCLUDED from the closed sync — see EXCLUDED_MLS below.
MLS_OPTIONS = [
    "GPS", "CRMLS", "CLAW", "SOUTHLAND",
    "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK",
]

# ── Excluded from CLOSED sync only ───────────────────────────────────────────
# Removed 2026-10-13. Scope matters: this list drives the historical CLOSED
# sales sync only. ITECH remains fully active in the on-market pipeline
# (unified/run-pipeline.py MLS_OPTIONS) and its listings keep syncing normally.
#
# Why: on the 2026-10-13 run, 7 of 8 MLSs succeeded and ITECH failed with
#   "fetch reported success but closed_0.0821y_ITECH_listings.json is missing"
# The fetch returns HTTP success but produces no closed-sales file, which is
# consistent with the Spark feed not granting this account closed/sold access
# for that association rather than with a bug on our side. The guard added in
# the same session caught it correctly and refused to seed a stale file, so the
# failure was loud rather than silent.
#
# ITECH is a very small association — ~165 KB of on-market data against
# CRMLS's 548 MB — so excluding its closed sales costs little, and keeping it
# in the list meant every weekly run reported a failure that needed no action.
# Revisit if closed access is confirmed for this feed.
EXCLUDED_MLS = {
    "ITECH": "no closed/sold feed access for this association (verified 2026-10-13)",
}

DEFAULT_WINDOW_DAYS = 30

# Converts --window-days into the --years argument the underlying fetch script
# expects. unified/closed/fetch.py uses fractional years fine because it
# applies CloseDate ge (now - timedelta(days=years*365.25)).
def _window_days_to_years(days: int) -> float:
    return round(days / 365.25, 4)

# Where the underlying scripts live — one dir down from this file
SCRIPT_DIR = Path(__file__).resolve().parent
UNIFIED_CLOSED_DIR = SCRIPT_DIR

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
PROJECT_ROOT = Path(__file__).resolve().parents[3]
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
    # Excluded names stay valid choices on purpose. Dropping them from choices
    # would make `--mls ITECH` fail with argparse's opaque "invalid choice",
    # leaving the next person to wonder where ITECH went. Accept it, then say
    # why it is skipped.
    selectable = MLS_OPTIONS + sorted(EXCLUDED_MLS)
    parser.add_argument("--mls", nargs="+", choices=selectable,
                        help="Restrict to specific MLS(s). Default: all non-excluded.")
    parser.add_argument("--exclude", nargs="+", choices=selectable,
                        help="Skip specific MLS(s).")
    args = parser.parse_args()

    years = _window_days_to_years(args.window_days)

    mls_list = args.mls or MLS_OPTIONS
    if args.exclude:
        mls_list = [m for m in mls_list if m not in set(args.exclude)]

    # Drop permanently-excluded feeds, stating the reason rather than failing
    # silently or reporting a failure every run.
    for m in [m for m in mls_list if m in EXCLUDED_MLS]:
        print(f"[SKIP] {m} is excluded from the closed sync: {EXCLUDED_MLS[m]}")
        print(f"       {m} is unaffected in the on-market pipeline "
              f"(unified/run-pipeline.py).", flush=True)
    mls_list = [m for m in mls_list if m not in EXCLUDED_MLS]

    if not mls_list:
        print("[ERROR] No MLSs left to process after exclusion filtering.", file=sys.stderr)
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
