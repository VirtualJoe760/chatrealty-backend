
import os
import json
import requests
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

BASE_URL = "https://replication.sparkapi.com/v1/listings"
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
EXPANSIONS = ["Rooms", "Units", "OpenHouses", "VirtualTours"]
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[4] / "local-logs"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
LISTINGS_FILE = LOCAL_LOGS_DIR / "all_listings_with_expansions.json"
SYNC_LOG_FILE = LOCAL_LOGS_DIR / "sync_log.json"

def clean_data(obj):
    """Clean data by removing null, empty, or sensitive values."""
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

def fetch_updated_listings(start_time, end_time, batch_size=500):
    """Fetch listings modified within the given time window."""
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }
    time_filter = f"ModificationTimestamp bt {start_time.isoformat()}Z,{end_time.isoformat()}Z"
    listings = []
    skiptoken = None
    page = 1
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_expand={','.join(EXPANSIONS)}&_filter={time_filter}"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        print(f"📄 Fetching updated listings, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        print("✅ No more updated listings to fetch")
                        break
                    cleaned = [clean_data(item) for item in batch]
                    listings.extend(cleaned)
                    skiptoken = batch[-1].get("Id")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    print(f"⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise Exception(f"❌ HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                print(f"⚠️ Request error: {e}")
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)  # Throttle to avoid rate limits

    return listings

def fetch_accessible_listing_keys(batch_size=1000):
    """Fetch all accessible ListingKey values."""
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }
    listing_keys = set()
    skiptoken = None
    page = 1
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_select=ListingKey"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        print(f"📄 Fetching listing keys, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        print("✅ No more listing keys to fetch")
                        break
                    listing_keys.update(item.get("ListingKey") for item in batch if item.get("ListingKey"))
                    skiptoken = batch[-1].get("ListingKey")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    print(f"⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise Exception(f"❌ HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                print(f"⚠️ Request error: {e}")
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)  # Throttle to avoid rate limits

    return listing_keys

def merge_listings(existing_listings, updated_listings):
    """Merge updated listings into existing listings by ListingKey."""
    listing_map = {listing.get("ListingKey"): listing for listing in existing_listings if listing.get("ListingKey")}
    for updated in updated_listings:
        listing_key = updated.get("ListingKey")
        if listing_key:
            listing_map[listing_key] = updated
    return list(listing_map.values())

def sync_listings(update_interval_hours=1, purge=False):
    """Sync local database by updating modified listings and optionally purging stale data."""
    # Load existing listings
    existing_listings = []
    if LISTINGS_FILE.exists():
        try:
            with LISTINGS_FILE.open("r", encoding="utf-8") as f:
                existing_listings = json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to read {LISTINGS_FILE}: {e}")

    # Fetch updated listings
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=update_interval_hours)
    updated_listings = fetch_updated_listings(start_time, end_time)
    print(f"📊 Fetched {len(updated_listings)} updated listings")

    # Merge updated listings
    merged_listings = merge_listings(existing_listings, updated_listings)

    # Purge stale data if requested
    if purge:
        accessible_keys = fetch_accessible_listing_keys()
        print(f"📊 Fetched {len(accessible_keys)} accessible listing keys")
        merged_listings = [
            listing for listing in merged_listings
            if listing.get("ListingKey") in accessible_keys
        ]
        print(f"🗑️ Purged {len(existing_listings) - len(merged_listings)} stale listings")

    # Save updated listings
    try:
        with LISTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(merged_listings, f, indent=2)
        print(f"✅ Saved {len(merged_listings)} listings to {LISTINGS_FILE}")
    except Exception as e:
        raise Exception(f"❌ Failed to write to {LISTINGS_FILE}: {e}")

    # Log sync operation
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "updated_listings": len(updated_listings),
        "total_listings": len(merged_listings),
        "purged": purge
    }
    try:
        sync_logs = []
        if SYNC_LOG_FILE.exists():
            with SYNC_LOG_FILE.open("r", encoding="utf-8") as f:
                sync_logs = json.load(f)
        sync_logs.append(log_entry)
        with SYNC_LOG_FILE.open("w", encoding="utf-8") as f:
            json.dump(sync_logs, f, indent=2)
        print(f"📝 Logged sync operation to {SYNC_LOG_FILE}")
    except Exception as e:
        print(f"⚠️ Failed to write to {SYNC_LOG_FILE}: {e}")

if __name__ == "__main__":
    try:
        # Run with purge=True daily, e.g., via cron at midnight
        # Run with purge=False hourly
        import sys
        purge = "--purge" in sys.argv
        sync_listings(purge=purge)
    except Exception as e:
        print(f"❌ Error in sync_listings.py: {e}")
        exit(1)