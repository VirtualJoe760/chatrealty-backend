#!/usr/bin/env python3
"""
Unified MLS Fetch Script - Production Ready

Fetches listings from multiple MLS associations via Spark Replication API.
Implements best practices from Spark documentation and Diego's guidance.

Features:
- Correct SkipToken pagination (uses API-provided token, not listing ID)
- Multi-MLS support (GPS, CRMLS, FlexMLS, etc.)
- Incremental updates via ModificationTimestamp
- Total count verification
- RESO-compliant field mapping

Usage:
    # Fetch all active listings from all MLSs
    python src/scripts/mls/backend/unified-fetch.py

    # Fetch from specific MLS
    python src/scripts/mls/backend/unified-fetch.py --mls GPS

    # Incremental update (last hour only)
    python src/scripts/mls/backend/unified-fetch.py --incremental
"""

import os
import json
import requests
import time
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

# Configuration
BASE_URL = "https://replication.sparkapi.com/v1/listings"
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[5] / "local-logs"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# MLS ID Mapping (from Diego's email - all 8 associations)
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
    """
    Print a progress bar with percentage and ETA

    Args:
        current: Current progress value
        total: Total value
        bar_length: Length of the progress bar
        prefix: Text to show before the progress bar
        start_time: Start time for ETA calculation
    """
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
    """Get total record count before fetching (per Diego's example)"""
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


def fetch_listings(
    mls_ids=None,
    property_types=["A", "B", "C", "D"],
    statuses=["Active"],
    incremental=False,
    incremental_hours=25,
    start_time=None,
    end_time=None,
    batch_size=500,
    expansions=None
):
    """
    Fetch listings from Spark Replication API

    Args:
        mls_ids: List of MLS names (e.g., ["GPS", "CRMLS"]) or None for all
        property_types: Property type codes (A=Residential, B=Lease, C=Multi-family, D=Land)
        statuses: StandardStatus values (Active, Pending, Closed, Expired)
        incremental: Use ModificationTimestamp for incremental updates
        incremental_hours: Lookback window when incremental=True (default 25h to
                           overlap the daily 6am cron by 1h)
        start_time: Start of time window (ISO format) — overrides incremental_hours
        end_time: End of time window (ISO format)
        batch_size: Records per request (max 1000 for replication API)
        expansions: List of expansions (e.g., ["Photos", "OpenHouses"])

    Returns:
        List of listing dictionaries
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

    # Status filter
    if statuses:
        status_conditions = [f"StandardStatus Eq '{s}'" for s in statuses]
        filter_parts.append("(" + " Or ".join(status_conditions) + ")")

    # Time-based filter (for incremental updates)
    if incremental or (start_time and end_time):
        if not start_time:
            # Default: last incremental_hours (25h = 1h overlap over the 24h cron interval)
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(hours=incremental_hours)

        start_iso = start_time.isoformat() + "Z" if isinstance(start_time, datetime) else start_time
        end_iso = end_time.isoformat() + "Z" if isinstance(end_time, datetime) else end_time

        # Spark uses 'bt' (between) operator for timestamp ranges
        filter_parts.append(f"ModificationTimestamp bt {start_iso},{end_iso}")

    combined_filter = " And ".join(filter_parts)

    # Build base URL
    url_params = [
        f"_limit={batch_size}",
        f"_filter={combined_filter}"
    ]

    if expansions:
        url_params.append(f"_expand={','.join(expansions)}")

    # Get total count first (Diego's recommendation)
    print(f"\n>>> Fetching from MLS(s): {', '.join(mls_ids)}")
    print(f">>> Filter: {combined_filter}\n")

    total_count = get_total_count(headers, combined_filter)
    if total_count is not None:
        print(f">>> Total records to fetch: {total_count:,}\n")

    # Fetch listings using skiptoken pagination
    all_listings = []
    skiptoken = ""  # Start with empty string (per Diego's guidance)
    page = 1
    retries = 3
    fetch_start_time = time.time()

    # Show initial progress bar if we have total count
    if total_count:
        print_progress_bar(0, total_count, prefix=f"Fetching {', '.join(mls_ids)}", start_time=fetch_start_time)

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

                    # CRITICAL: Get SkipToken from API response (not from listing ID!)
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
                        print_progress_bar(len(all_listings), total_count, prefix=f"Fetching {', '.join(mls_ids)}", start_time=fetch_start_time)
                    else:
                        print(f"[Page {page}] Fetched {len(batch)} listings (Total: {len(all_listings):,})")

                    # Check for end condition (per Diego's REPLICATION_GUIDE.md)
                    if not new_skiptoken or new_skiptoken == skiptoken:
                        print(f"[Page {page}] SkipToken unchanged. Fetch complete.\n")
                        success = True
                        break

                    skiptoken = new_skiptoken
                    success = True
                    break

                elif response.status_code == 429:
                    wait_time = 3 + (attempt * 2)
                    print(f"[Page {page}] Rate limited. Waiting {wait_time}s...")
                    time.sleep(wait_time)

                elif response.status_code == 400 and "Media" in response.text and "_expand=Media" in url:
                    # Some MLS feeds (BRIDGE, ITECH, possibly others) reject Media
                    # expansion. Drop it and retry the same page.
                    print(f"[Page {page}] {mls_ids} does not support _expand=Media — retrying without it")
                    url = url.replace("_expand=Media,OpenHouses,VirtualTours", "_expand=OpenHouses,VirtualTours")
                    # rebuild url_params so subsequent pages also drop Media
                    url_params = [p.replace("_expand=Media,OpenHouses,VirtualTours",
                                            "_expand=OpenHouses,VirtualTours") for p in url_params]

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
        time.sleep(0.2)  # Rate limiting courtesy

    # Verify count
    if total_count is not None:
        if len(all_listings) != total_count:
            print(f"[WARN] Count mismatch! Expected {total_count:,}, got {len(all_listings):,}")
        else:
            print(f"[OK] Count verified: {len(all_listings):,} records")

    return all_listings


def save_to_file(listings, mls_names, incremental=False):
    """Save listings to JSON file"""
    if incremental:
        filename = f"incremental_{'_'.join(mls_names)}_listings.json"
    else:
        filename = f"all_{'_'.join(mls_names)}_listings.json"

    output_file = LOCAL_LOGS_DIR / filename

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(listings, f, indent=2)

        print(f"\n>>> Saved {len(listings):,} listings to: {output_file}")
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
    parser = argparse.ArgumentParser(description="Fetch MLS listings from Spark Replication API")
    parser.add_argument(
        "--mls",
        nargs="+",
        choices=list(MLS_IDS.keys()),
        help="MLS to fetch. Default: all in sequence with prompts"
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Auto-confirm all prompts (yes to all MLSs)"
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Fetch only listings modified in the last N hours (see --hours, default 25)"
    )
    parser.add_argument(
        "--status",
        nargs="+",
        default=["Active", "Active Under Contract", "Pending", "Hold", "ComingSoon"],
        help="StandardStatus filter. Default covers the full on-market lifecycle so "
             "the seed router can place each doc in the right collection "
             "(unified_listings / unified_in_escrow / unified_off_market)."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Records per API request (max 1000)"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=25,
        help="Incremental lookback window in hours (default 25 — overlaps the daily 6am cron by 1h)"
    )

    args = parser.parse_args()

    # Determine MLS list to process
    if args.mls:
        mls_list = args.mls
    else:
        mls_list = list(MLS_IDS.keys())  # All 8 MLSs

    try:
        print("=" * 80)
        print("Unified MLS Fetch - Spark Replication API")
        print("=" * 80)
        print(f"Mode: {'Auto-confirm (--yes)' if args.yes else 'Interactive prompts'}")
        print(f"MLSs to fetch: {', '.join(mls_list)}")
        print(f"Total MLSs: {len(mls_list)}")
        print("=" * 80 + "\n")

        total_fetched = 0
        completed_mls = []
        skipped_mls = []

        # Process each MLS sequentially
        for idx, mls_name in enumerate(mls_list, 1):
            print(f"\n{'#' * 80}")
            print(f"# MLS {idx}/{len(mls_list)}: {mls_name}")
            print(f"{'#' * 80}\n")

            # Prompt for confirmation (unless --yes flag or first MLS)
            if idx > 1 and not args.yes:
                if not prompt_continue(mls_name, args.yes):
                    print(f"[SKIPPED] {mls_name}")
                    skipped_mls.append(mls_name)
                    continue

            # Fetch from this MLS
            try:
                listings = fetch_listings(
                    mls_ids=[mls_name],  # Single MLS at a time
                    statuses=args.status,
                    incremental=args.incremental,
                    incremental_hours=args.hours,
                    batch_size=args.batch_size,
                    expansions=["Media", "OpenHouses", "VirtualTours"]
                )

                if not listings:
                    print(f"\n[WARN] No listings fetched from {mls_name}")
                    continue

                # Save to individual MLS file
                output_file = save_to_file(listings, [mls_name], args.incremental)

                # Track progress
                total_fetched += len(listings)
                completed_mls.append(mls_name)

                # MLS Summary
                print("\n" + "-" * 80)
                print(f"{mls_name} Summary:")
                print(f"  Fetched: {len(listings):,}")
                print(f"  Output: {output_file}")
                print("-" * 80)

            except Exception as e:
                print(f"\n[ERROR] Failed to fetch {mls_name}: {e}")
                skipped_mls.append(mls_name)
                continue

        # Final Summary
        print("\n\n" + "=" * 80)
        print("FINAL SUMMARY")
        print("=" * 80)
        print(f"Completed: {len(completed_mls)}/{len(mls_list)} MLSs")
        if completed_mls:
            print(f"  - {', '.join(completed_mls)}")
        if skipped_mls:
            print(f"Skipped: {len(skipped_mls)} MLSs")
            print(f"  - {', '.join(skipped_mls)}")
        print(f"Total listings fetched: {total_fetched:,}")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        exit(1)


if __name__ == "__main__":
    main()
