#!/usr/bin/env python3
"""
Unified MLS Pipeline Runner

Orchestrates the complete data pipeline:
1. Fetch listings from Spark API (unified-fetch.py)
2. Flatten to camelCase with enhanced fields (flatten.py)
3. Seed to MongoDB unified_listings collection (seed.py)

Usage:
    # Run full pipeline for GPS MLS
    python src/scripts/mls/backend/unified/run-pipeline.py --mls GPS

    # Run full pipeline for all 8 MLSs
    python src/scripts/mls/backend/unified/run-pipeline.py --all

    # Run only specific steps
    python src/scripts/mls/backend/unified/run-pipeline.py --mls GPS --steps fetch,flatten
    python src/scripts/mls/backend/unified/run-pipeline.py --mls GPS --steps seed

    # Incremental update (fetch only recent changes)
    python src/scripts/mls/backend/unified/run-pipeline.py --all --incremental
"""

import subprocess
import argparse
import sys
from pathlib import Path
from datetime import datetime

# Available MLSs
MLS_OPTIONS = ["GPS", "CRMLS", "CLAW", "SOUTHLAND", "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK", "ITECH"]


def run_command(cmd, description):
    """Run a command and handle errors"""
    print(f"\n{'=' * 80}")
    print(f"{description}")
    print(f"{'=' * 80}")
    print(f"Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=False, text=True)

    if result.returncode != 0:
        print(f"\n[ERROR] {description} failed with exit code {result.returncode}")
        return False

    print(f"\n[OK] {description} completed successfully")
    return True


def run_pipeline(mls_list, steps, incremental=False):
    """Run the unified MLS pipeline"""
    project_root = Path(__file__).resolve().parents[5]
    scripts_dir = project_root / "src/scripts/mls/backend/unified"

    start_time = datetime.now()
    print("\n" + "=" * 80)
    print("UNIFIED MLS PIPELINE")
    print("=" * 80)
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"MLSs: {', '.join(mls_list)}")
    print(f"Steps: {', '.join(steps)}")
    print(f"Incremental: {incremental}")
    print("=" * 80)

    for mls in mls_list:
        print(f"\n\n{'#' * 80}")
        print(f"# Processing MLS: {mls}")
        print(f"{'#' * 80}")

        # Step 1: Fetch
        if "fetch" in steps:
            fetch_cmd = [
                sys.executable,
                str(scripts_dir / "unified-fetch.py"),
                "--mls", mls
            ]
            if incremental:
                fetch_cmd.append("--incremental")

            if not run_command(fetch_cmd, f"Fetch listings from {mls}"):
                print(f"\n[ERROR] Pipeline failed at fetch step for {mls}")
                return False

        # Step 2: Flatten
        if "flatten" in steps:
            flatten_cmd = [
                sys.executable,
                str(scripts_dir / "flatten.py")
            ]
            # flatten.py auto-detects the most recent file, so no need to specify input

            if not run_command(flatten_cmd, f"Flatten listings for {mls}"):
                print(f"\n[ERROR] Pipeline failed at flatten step for {mls}")
                return False

        # Step 3: Seed
        if "seed" in steps:
            seed_cmd = [
                sys.executable,
                str(scripts_dir / "seed.py")
            ]
            # seed.py auto-detects the most recent flattened file

            if not run_command(seed_cmd, f"Seed listings to MongoDB for {mls}"):
                print(f"\n[ERROR] Pipeline failed at seed step for {mls}")
                return False

    end_time = datetime.now()
    duration = end_time - start_time

    print("\n\n" + "=" * 80)
    print("PIPELINE COMPLETE")
    print("=" * 80)
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Ended: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {duration}")
    print(f"MLSs processed: {len(mls_list)}")
    print("=" * 80 + "\n")

    return True


def main():
    parser = argparse.ArgumentParser(description="Run unified MLS data pipeline")
    parser.add_argument(
        "--mls",
        nargs="+",
        choices=MLS_OPTIONS,
        help="MLS(s) to process (GPS, CRMLS, etc.)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all 8 MLSs"
    )
    parser.add_argument(
        "--steps",
        type=str,
        default="fetch,flatten,seed",
        help="Pipeline steps to run (comma-separated: fetch,flatten,seed). Default: all steps"
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Fetch only recent changes (last hour)"
    )

    args = parser.parse_args()

    # Determine MLS list
    if args.all:
        mls_list = MLS_OPTIONS
    elif args.mls:
        mls_list = args.mls
    else:
        print("[ERROR] Must specify either --mls or --all")
        parser.print_help()
        sys.exit(1)

    # Parse steps
    steps = [s.strip() for s in args.steps.split(",")]
    valid_steps = {"fetch", "flatten", "seed"}
    if not all(s in valid_steps for s in steps):
        print(f"[ERROR] Invalid steps. Valid options: {', '.join(valid_steps)}")
        sys.exit(1)

    # Run pipeline
    try:
        success = run_pipeline(mls_list, steps, args.incremental)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n[WARN] Pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n[ERROR] Pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
