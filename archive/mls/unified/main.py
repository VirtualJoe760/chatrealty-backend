#!/usr/bin/env python3
"""
Main Daily MLS Update Pipeline

Orchestrates the complete daily update workflow for all 8 MLSs:
1. Fetch new/updated listings (incremental updates)
2. Flatten listings to camelCase
3. Seed to MongoDB (unified_listings collection)
4. Update listing statuses (Active → Pending → Closed)

Designed to run daily via cron job at 6:00 AM.

Usage:
    # Full daily update (recommended for cron)
    python src/scripts/mls/backend/unified/main.py

    # Skip status updates (fetch + seed only)
    python src/scripts/mls/backend/unified/main.py --skip-status-update

    # Specific MLSs only
    python src/scripts/mls/backend/unified/main.py --mls GPS CRMLS

    # Dry run (no database writes)
    python src/scripts/mls/backend/unified/main.py --dry-run
"""

import sys
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
import time

# Colors for terminal output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def print_header(message):
    """Print a formatted header"""
    print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{message.center(80)}{Colors.ENDC}")
    print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}\n")

def print_step(step_num, total_steps, message):
    """Print a step indicator"""
    print(f"\n{Colors.OKCYAN}[Step {step_num}/{total_steps}] {message}{Colors.ENDC}")
    print(f"{Colors.OKCYAN}{'-'*80}{Colors.ENDC}\n")

def run_script(script_path, args=None, description=""):
    """
    Run a Python script and capture output

    Args:
        script_path: Path to the script
        args: List of command-line arguments
        description: Human-readable description

    Returns:
        bool: True if successful, False otherwise
    """
    if not script_path.exists():
        print(f"{Colors.FAIL}❌ Script not found: {script_path}{Colors.ENDC}")
        return False

    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)

    print(f"{Colors.OKBLUE}Running: {' '.join(cmd)}{Colors.ENDC}\n")

    try:
        start_time = time.time()

        result = subprocess.run(
            cmd,
            check=True,
            capture_output=False,  # Show output in real-time
            text=True
        )

        elapsed = time.time() - start_time
        print(f"\n{Colors.OKGREEN}✅ {description} completed in {elapsed:.1f}s{Colors.ENDC}")
        return True

    except subprocess.CalledProcessError as e:
        print(f"\n{Colors.FAIL}❌ {description} failed with exit code {e.returncode}{Colors.ENDC}")
        return False
    except Exception as e:
        print(f"\n{Colors.FAIL}❌ {description} failed: {e}{Colors.ENDC}")
        return False

def main():
    parser = argparse.ArgumentParser(
        description="Daily MLS update pipeline for all 8 MLSs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full daily update (for cron)
  python main.py

  # Update specific MLSs only
  python main.py --mls GPS CRMLS

  # Skip status updates
  python main.py --skip-status-update

  # Dry run (no database writes)
  python main.py --dry-run
        """
    )

    parser.add_argument(
        "--mls",
        nargs="+",
        choices=["GPS", "CRMLS", "CLAW", "SOUTHLAND", "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK", "ITECH"],
        help="Specific MLSs to update (default: all 8 MLSs)"
    )

    parser.add_argument(
        "--skip-status-update",
        action="store_true",
        help="Skip status update step (only fetch + seed new listings)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode (no database writes)"
    )

    parser.add_argument(
        "--incremental",
        action="store_true",
        default=True,
        help="Incremental update (last 24 hours only) - DEFAULT"
    )

    parser.add_argument(
        "--full-refetch",
        action="store_true",
        help="Full refetch (all active listings, not just recent changes)"
    )

    args = parser.parse_args()

    # Paths
    script_dir = Path(__file__).parent
    unified_fetch = script_dir / "unified-fetch.py"
    flatten = script_dir / "flatten.py"
    seed = script_dir / "seed.py"
    update_status = script_dir / "update-status.py"

    # Determine mode
    mode = "FULL REFETCH" if args.full_refetch else "INCREMENTAL UPDATE"
    mls_list = args.mls if args.mls else ["all 8 MLSs"]

    # Print header
    print_header("Daily MLS Update Pipeline")
    print(f"{Colors.BOLD}Started:{Colors.ENDC} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{Colors.BOLD}Mode:{Colors.ENDC} {mode}")
    print(f"{Colors.BOLD}MLSs:{Colors.ENDC} {', '.join(mls_list)}")
    print(f"{Colors.BOLD}Status Update:{Colors.ENDC} {'No (skipped)' if args.skip_status_update else 'Yes'}")
    print(f"{Colors.BOLD}Dry Run:{Colors.ENDC} {'Yes' if args.dry_run else 'No'}")

    if args.dry_run:
        print(f"\n{Colors.WARNING}⚠️  DRY RUN MODE - No database writes will occur{Colors.ENDC}")

    # Determine steps
    total_steps = 3 if args.skip_status_update else 4
    current_step = 0
    failed_steps = []

    # ========================================================================
    # STEP 1: FETCH NEW/UPDATED LISTINGS
    # ========================================================================
    current_step += 1
    print_step(current_step, total_steps, "Fetch New/Updated Listings")

    fetch_args = ["--yes"]  # Auto-confirm

    if args.mls:
        fetch_args.extend(["--mls"] + args.mls)

    if args.full_refetch:
        # Full refetch (don't use --incremental flag)
        pass
    else:
        # Incremental (last 24 hours)
        fetch_args.append("--incremental")

    if not run_script(unified_fetch, fetch_args, "Fetch listings"):
        failed_steps.append("Fetch")
        print(f"\n{Colors.FAIL}❌ Pipeline failed at Step {current_step}: Fetch{Colors.ENDC}")
        sys.exit(1)

    # ========================================================================
    # STEP 2: FLATTEN LISTINGS
    # ========================================================================
    current_step += 1
    print_step(current_step, total_steps, "Flatten Listings to camelCase")

    # Flatten will auto-detect the most recent fetched file
    if not run_script(flatten, None, "Flatten listings"):
        failed_steps.append("Flatten")
        print(f"\n{Colors.FAIL}❌ Pipeline failed at Step {current_step}: Flatten{Colors.ENDC}")
        sys.exit(1)

    # ========================================================================
    # STEP 3: SEED TO MONGODB
    # ========================================================================
    current_step += 1
    print_step(current_step, total_steps, "Seed to MongoDB (unified_listings)")

    if args.dry_run:
        print(f"{Colors.WARNING}⚠️  Skipping seed (dry run mode){Colors.ENDC}")
    else:
        # Seed will auto-detect the most recent flattened file
        if not run_script(seed, None, "Seed to MongoDB"):
            failed_steps.append("Seed")
            print(f"\n{Colors.FAIL}❌ Pipeline failed at Step {current_step}: Seed{Colors.ENDC}")
            sys.exit(1)

    # ========================================================================
    # STEP 4: UPDATE LISTING STATUSES
    # ========================================================================
    if not args.skip_status_update:
        current_step += 1
        print_step(current_step, total_steps, "Update Listing Statuses (Active → Pending → Closed)")

        if args.dry_run:
            print(f"{Colors.WARNING}⚠️  Skipping status update (dry run mode){Colors.ENDC}")
        else:
            if not run_script(update_status, None, "Update listing statuses"):
                failed_steps.append("Status Update")
                print(f"\n{Colors.WARNING}⚠️  Status update failed but pipeline will continue{Colors.ENDC}")
                # Don't exit - status update failures shouldn't block the pipeline

    # ========================================================================
    # FINAL SUMMARY
    # ========================================================================
    print_header("Pipeline Complete")

    print(f"{Colors.BOLD}Finished:{Colors.ENDC} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{Colors.BOLD}Mode:{Colors.ENDC} {mode}")
    print(f"{Colors.BOLD}MLSs Updated:{Colors.ENDC} {', '.join(mls_list)}")

    if failed_steps:
        print(f"\n{Colors.WARNING}⚠️  Some steps failed:{Colors.ENDC}")
        for step in failed_steps:
            print(f"   - {step}")
        sys.exit(1)
    else:
        print(f"\n{Colors.OKGREEN}✅ All steps completed successfully!{Colors.ENDC}")

        if not args.dry_run:
            print(f"\n{Colors.OKGREEN}✨ Database updated with latest MLS data{Colors.ENDC}")
        else:
            print(f"\n{Colors.WARNING}ℹ️  Dry run complete - no database changes made{Colors.ENDC}")

    print()
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{Colors.WARNING}🛑 Pipeline interrupted by user{Colors.ENDC}\n")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n{Colors.FAIL}❌ Pipeline failed with error: {e}{Colors.ENDC}\n")
        sys.exit(1)
