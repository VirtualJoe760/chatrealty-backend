#!/usr/bin/env python3
"""
Unified MLS Fetch Script - CLOSED LISTINGS (Past 5 Years)

Fetches closed/sold listings from all 8 MLS associations via Spark Replication API.
Mirrors the structure of unified-fetch.py but targets StandardStatus='Closed'.

Data Retention: Only fetches closed listings from past 5 years.

Features:
- Correct SkipToken pagination (uses API-provided token)
- Multi-MLS support (all 8 associations)
- 5-year lookback window
- RESO-compliant field mapping
- Saves to unified_closed_listings collection

Usage:
    # Fetch all closed listings from past 5 years (all MLSs)
    python src/scripts/mls/backend/unified/closed/fetch.py

    # Fetch from specific MLS
    python src/scripts/mls/backend/unified/closed/fetch.py --mls GPS

    # Exclude slow MLSs (GPS, CRMLS) and fetch the rest
    python src/scripts/mls/backend/unified/closed/fetch.py --exclude GPS CRMLS -y

    # Custom time range
    python src/scripts/mls/backend/unified/closed/fetch.py --years 3

    # Auto-confirm all prompts (no interaction)
    python src/scripts/mls/backend/unified/closed/fetch.py -y

    # Combine exclude and auto-confirm
    python src/scripts/mls/backend/unified/closed/fetch.py --exclude GPS CRMLS -y --delay 1.5
"""

import os
import json
import requests
import time
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[6] / ".env.local"
load_dotenv(dotenv_path=env_path)

# Configuration
BASE_URL = "https://replication.sparkapi.com/v1/listings"
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[6] / "local-logs" / "closed"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# MLS ID Mapping (all 8 associations - same as unified-fetch.py)
MLS_IDS = {
    "GPS": "20190211172710340762000000",
    "CRMLS": "20200218121507636729000000",
    "CLAW": "20200630203341057545000000",
    "SOUTHLAND": "20200630203518576361000000",
    "HIGH_DESERT": "20200630204544040064000000",
    "BRIDGE": "20200630204733042221000000",
    "CONEJO_SIMI_MOORPARK": "20160622112753445171000000",
    "ITECH": "20200630203206752718000000"
}


def clean_data(obj):
    """Remove null, empty, and masked fields"""
    if isinstance(obj, dict):
        return {
            k: clean_data(v)
            for k, v in obj.items()
            if v not in (None, "********", [], {})
        }
    elif isinstance(obj, list):
        return [clean_data(v) for v in obj if v not in (None, "********", [], {})]
    else:
        return obj


def format_time(seconds):
    """Format seconds into human-readable time"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        mins = seconds / 60
        return f"{mins:.1f}m"
    else:
        hours = seconds / 3600
        mins = (seconds % 3600) / 60
        return f"{hours:.0f}h {mins:.0f}m"


def print_progress_bar(current, total, bar_length=50, prefix="Progress", start_time=None):
    """Print a progress bar with percentage and ETA"""
    if total == 0:
        return

    percent = (current / total) * 100
    filled = int(bar_length * current / total)
    bar = '=' * filled + '-' * (bar_length - filled)

    # Calculate ETA
    eta_str = ""
    if start_time and current > 0:
        elapsed = time.time() - start_time
        avg_time_per_item = elapsed / current
        remaining = total - current
        eta_seconds = avg_time_per_item * remaining
        eta_str = f" | ETA: {format_time(eta_seconds)}"

    # Print with carriage return to overwrite
    sys.stdout.write(f"\r{prefix}: [{bar}] {percent:.1f}% ({current:,}/{total:,}){eta_str}")
    sys.stdout.flush()

    # Print newline when complete
    if current >= total:
        print()


def get_total_count(headers, filter_query):
    """Get total record count before fetching"""
    count_url = f"{BASE_URL}?_filter={filter_query}&_pagination=count"

    try:
        response = requests.get(count_url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            total_rows = data.get("D", {}).get("Pagination", {}).get("TotalRows", 0)
            return total_rows
        else:
            print(f"[WARN] Could not get count: {response.status_code}")
            return None
    except Exception as e:
        print(f"[WARN] Count request failed: {e}")
        return None


def fetch_closed_listings(
    mls_ids=None,
    property_types=["A", "B", "C", "D"],
    years_back=5,
    batch_size=500,
    expansions=None,
    rate_limit_delay=1.0
):
    """
    Fetch closed listings from Spark Replication API

    Args:
        mls_ids: List of MLS names (e.g., ["GPS", "CRMLS"]) or None for all
        property_types: Property type codes (A=Residential, B=Lease, C=Multi-family, D=Land)
        years_back: Number of years to look back (default: 5)
        batch_size: Records per request (max 1000 for replication API)
        expansions: List of expansions (e.g., ["Media", "OpenHouses"])
        rate_limit_delay: Seconds to wait between requests (default: 1.0)

    Returns:
        List of closed listing dictionaries
    """

    if not ACCESS_TOKEN:
        raise Exception("[ERROR] SPARK_ACCESS_TOKEN is missing in .env.local")

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }

    # Default to all MLSs if none specified
    if mls_ids is None:
        mls_ids = list(MLS_IDS.keys())

    # Validate MLS IDs
    for mls_name in mls_ids:
        if mls_name not in MLS_IDS:
            raise ValueError(f"Unknown MLS: {mls_name}. Available: {list(MLS_IDS.keys())}")

    # Build filter
    filter_parts = []

    # MLS filter
    if len(mls_ids) == 1:
        mls_filter = f"MlsId Eq '{MLS_IDS[mls_ids[0]]}'"
    else:
        mls_conditions = [f"MlsId Eq '{MLS_IDS[name]}'" for name in mls_ids]
        mls_filter = "(" + " Or ".join(mls_conditions) + ")"
    filter_parts.append(mls_filter)

    # Property type filter
    if property_types:
        type_conditions = [f"PropertyType Eq '{t}'" for t in property_types]
        filter_parts.append("(" + " Or ".join(type_conditions) + ")")

    # ⭐ CLOSED STATUS FILTER (key difference from unified-fetch.py)
    filter_parts.append("StandardStatus Eq 'Closed'")

    # ⭐ DATE FILTER - Only fetch closed listings from past X years
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=365.25 * years_back)
    cutoff_iso = cutoff_date.isoformat().replace('+00:00', 'Z')
    filter_parts.append(f"CloseDate ge {cutoff_iso}")

    combined_filter = " And ".join(filter_parts)

    # Build base URL
    url_params = [
        f"_limit={batch_size}",
        f"_filter={combined_filter}"
    ]

    # Only add expansions if provided and not empty
    if expansions and len(expansions) > 0:
        url_params.append(f"_expand={','.join(expansions)}")

    # Get total count first
    print(f"\n>>> Fetching CLOSED listings from MLS(s): {', '.join(mls_ids)}")
    print(f">>> Lookback period: Past {years_back} years (since {cutoff_date.strftime('%Y-%m-%d')})")
    print(f">>> Filter: {combined_filter}\n")

    total_count = get_total_count(headers, combined_filter)
    if total_count is not None:
        print(f">>> Total closed sales to fetch: {total_count:,}\n")

    # Fetch listings using skiptoken pagination
    all_listings = []
    skiptoken = ""  # Start with empty string
    page = 1
    retries = 3
    fetch_start_time = time.time()

    # Show initial progress bar if we have total count
    if total_count:
        print_progress_bar(0, total_count, prefix=f"Fetching {', '.join(mls_ids)} CLOSED", start_time=fetch_start_time)

    while True:
        # Build URL with skiptoken
        url = f"{BASE_URL}?{'&'.join(url_params)}&_skiptoken={skiptoken}"

        # Initialize variables before retry loop
        batch = []
        new_skiptoken = None
        success = False

        for attempt in range(retries):
            try:
                response = requests.get(url, headers=headers, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    response_data = data.get("D", {})
                    batch = response_data.get("Results", [])

                    # CRITICAL: Get SkipToken from API response
                    new_skiptoken = response_data.get("SkipToken")

                    if not batch:
                        print(f"[Page {page}] No more results. Fetch complete.\n")
                        success = True
                        break

                    # Clean and add to results
                    cleaned_batch = [clean_data(listing) for listing in batch]
                    all_listings.extend(cleaned_batch)

                    # Update progress bar
                    if total_count:
                        print_progress_bar(len(all_listings), total_count, prefix=f"Fetching {', '.join(mls_ids)} CLOSED", start_time=fetch_start_time)
                    else:
                        print(f"[Page {page}] Fetched {len(batch)} closed listings (Total: {len(all_listings):,})")

                    # Check for end condition
                    if not new_skiptoken or new_skiptoken == skiptoken:
                        print(f"[Page {page}] SkipToken unchanged. Fetch complete.\n")
                        success = True
                        break

                    skiptoken = new_skiptoken
                    success = True
                    break

                elif response.status_code == 429:
                    # Exponential backoff for rate limiting
                    wait_time = min(30, 5 + (attempt * 5))  # Cap at 30 seconds
                    print(f"\n[Page {page}] Rate limited (429). Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)

                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    raise Exception(error_msg)

            except requests.RequestException as e:
                print(f"[Page {page}] Request error: {e}")
                if attempt == retries - 1:
                    raise Exception(f"Max retries reached: {e}")
                time.sleep(2 ** attempt)

        # Break if request failed or no batch
        if not success or not batch:
            break

        page += 1
        # Configurable rate limiting delay
        if rate_limit_delay > 0:
            time.sleep(rate_limit_delay)

    # Verify count
    if total_count is not None:
        if len(all_listings) != total_count:
            print(f"[WARN] Count mismatch! Expected {total_count:,}, got {len(all_listings):,}")
        else:
            print(f"[OK] Count verified: {len(all_listings):,} records")

    return all_listings


def save_to_file(listings, mls_names, years_back=5):
    """Save listings to JSON file"""
    filename = f"closed_{years_back}y_{'_'.join(mls_names)}_listings.json"
    output_file = LOCAL_LOGS_DIR / filename

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(listings, f, indent=2)

        print(f"\n>>> Saved {len(listings):,} closed listings to: {output_file}")
        return output_file
    except Exception as e:
        raise Exception(f"Failed to write to {output_file}: {e}")


def prompt_continue(mls_name, auto_yes=False):
    """Prompt user to continue with next MLS"""
    if auto_yes:
        return True

    while True:
        response = input(f"\nContinue to {mls_name}? [y/n]: ").strip().lower()
        if response in ['y', 'yes']:
            return True
        elif response in ['n', 'no']:
            return False
        else:
            print("Please enter 'y' or 'n'")


def main():
    parser = argparse.ArgumentParser(description="Fetch CLOSED MLS listings from Spark Replication API")
    parser.add_argument(
        "--mls",
        nargs="+",
        choices=list(MLS_IDS.keys()),
        help="MLS to fetch. Default: all in sequence with prompts"
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        choices=list(MLS_IDS.keys()),
        help="MLS associations to exclude from fetch (e.g., --exclude GPS CRMLS)"
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Auto-confirm all prompts (yes to all MLSs)"
    )
    parser.add_argument(
        "--years",
        type=float,
        default=5,
        help="Number of years to look back (default: 5). Accepts fractional values "
             "so the daily historical_closed_sync.py can request sub-year windows "
             "(e.g., --years 0.0821 for a 30-day rolling window)."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Records per API request (max 1000, default: 1000)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between API requests (default: 2.0)"
    )

    args = parser.parse_args()

    # Determine MLS list to process
    if args.mls:
        mls_list = args.mls
    else:
        mls_list = list(MLS_IDS.keys())  # All 8 MLSs

    # Apply exclusions if specified
    if args.exclude:
        excluded = set(args.exclude)
        mls_list = [mls for mls in mls_list if mls not in excluded]
        if not mls_list:
            print("[ERROR] All MLSs were excluded. Nothing to fetch.")
            exit(1)

    try:
        print("=" * 80)
        print("Unified MLS Fetch - CLOSED LISTINGS (Past 5 Years)")
        print("=" * 80)
        print(f"Mode: {'Auto-confirm (--yes)' if args.yes else 'Interactive prompts'}")
        print(f"MLSs to fetch: {', '.join(mls_list)}")
        print(f"Total MLSs: {len(mls_list)}")
        if args.exclude:
            print(f"Excluded MLSs: {', '.join(args.exclude)}")
        print(f"Lookback period: {args.years} years")
        print(f"Batch size: {args.batch_size} records/request")
        print(f"Rate limit delay: {args.delay}s between requests")
        print("=" * 80 + "\n")

        total_fetched = 0
        completed_mls = []
        skipped_mls = []

        # Process each MLS sequentially
        for idx, mls_name in enumerate(mls_list, 1):
            print(f"\n{'#' * 80}")
            print(f"# MLS {idx}/{len(mls_list)}: {mls_name} - CLOSED LISTINGS")
            print(f"{'#' * 80}\n")

            # Prompt for confirmation (unless --yes flag or first MLS)
            if idx > 1 and not args.yes:
                if not prompt_continue(mls_name, args.yes):
                    print(f"[SKIPPED] {mls_name}")
                    skipped_mls.append(mls_name)
                    continue

            # Fetch from this MLS
            try:
                listings = fetch_closed_listings(
                    mls_ids=[mls_name],  # Single MLS at a time
                    years_back=args.years,
                    batch_size=args.batch_size,
                    expansions=None,  # Media expansion not supported for closed listings
                    rate_limit_delay=args.delay
                )

                if not listings:
                    print(f"\n[WARN] No closed listings fetched from {mls_name}")
                    continue

                # Save to individual MLS file
                output_file = save_to_file(listings, [mls_name], args.years)

                # Track progress
                total_fetched += len(listings)
                completed_mls.append(mls_name)

                # MLS Summary
                print("\n" + "-" * 80)
                print(f"{mls_name} CLOSED LISTINGS Summary:")
                print(f"  Fetched: {len(listings):,}")
                print(f"  Output: {output_file}")
                print("-" * 80)

            except Exception as e:
                print(f"\n[ERROR] Failed to fetch {mls_name}: {e}")
                skipped_mls.append(mls_name)
                continue

        # Final Summary
        print("\n\n" + "=" * 80)
        print("FINAL SUMMARY - CLOSED LISTINGS")
        print("=" * 80)
        print(f"Completed: {len(completed_mls)}/{len(mls_list)} MLSs")
        if completed_mls:
            print(f"  - {', '.join(completed_mls)}")
        if skipped_mls:
            print(f"Skipped: {len(skipped_mls)} MLSs")
            print(f"  - {', '.join(skipped_mls)}")
        print(f"Total closed sales fetched: {total_fetched:,}")
        print(f"Lookback period: Past {args.years} years")
        print("\n[*] Next step: Run seed script to upload to MongoDB (unified_closed_listings collection)")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        exit(1)


if __name__ == "__main__":
    main()
