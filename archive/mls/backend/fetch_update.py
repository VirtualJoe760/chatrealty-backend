import os
import json
import time
import requests
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta, UTC

# Load env
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

BASE_URL = "https://replication.sparkapi.com/v1/listings"
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
EXPANSIONS = ["Rooms", "Units", "OpenHouses", "VirtualTours"]
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[4] / "local-logs"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOCAL_LOGS_DIR / "all_listings_with_expansions.json"
RUN_ID = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOCAL_LOGS_DIR / f"sync_run_{RUN_ID}.jsonl"

def append_log(entry: dict) -> None:
    """Append a log entry to the JSONL log file."""
    entry = {"ts": datetime.now(UTC).isoformat(), **entry}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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

def fetch_updated_listings(start_time: datetime, end_time: datetime, batch_size: int = 500) -> list:
    """Fetch listings modified within the given time window."""
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }
    # Format timestamps without microseconds
    start_str = start_time.replace(microsecond=0).isoformat() + "Z"
    end_str = end_time.replace(microsecond=0).isoformat() + "Z"
    time_filter = f"ModificationTimestamp bt {start_str},{end_str}"
    time_filter_encoded = urllib.parse.quote(time_filter)
    listings = []
    skiptoken = None
    page = 1
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_expand={','.join(EXPANSIONS)}&_filter={time_filter_encoded}"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        append_log({"event": "fetch_updated_listings", "page": page, "url": url})
        print(f"📄 Fetching updated listings, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        append_log({"event": "fetch_updated_listings_complete", "page": page, "listings_fetched": len(listings)})
                        print("✅ No more updated listings to fetch")
                        break
                    cleaned = [clean_data(item) for item in batch]
                    listings.extend(cleaned)
                    skiptoken = batch[-1].get("Id")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    append_log({"event": "rate_limited", "page": page, "wait_seconds": wait})
                    print(f"⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    append_log({"event": "fetch_error", "page": page, "status_code": res.status_code, "response": res.text})
                    raise Exception(f"❌ HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                append_log({"event": "fetch_error", "page": page, "error": str(e)})
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)  # Increased throttle

    return listings

def fetch_accessible_listing_keys(batch_size: int = 1000) -> set:
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

        append_log({"event": "fetch_listing_keys", "page": page, "url": url})
        print(f"📄 Fetching listing keys, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        append_log({"event": "fetch_listing_keys_complete", "page": page, "keys_fetched": len(listing_keys)})
                        print("✅ No more listing keys to fetch")
                        break
                    listing_keys.update(item.get("ListingKey") for item in batch if item.get("ListingKey"))
                    skiptoken = batch[-1].get("ListingKey")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    append_log({"event": "rate_limited", "page": page, "wait_seconds": wait})
                    print(f"⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    append_log({"event": "fetch_error", "page": page, "status_code": res.status_code, "response": res.text})
                    raise Exception(f"❌ HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                append_log({"event": "fetch_error", "page": page, "error": str(e)})
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)

    return listing_keys

def merge_listings(existing_listings: list, updated_listings: list) -> list:
    """Merge updated listings into existing listings by ListingKey."""
    listing_map = {listing.get("ListingKey"): listing for listing in existing_listings if listing.get("ListingKey")}
    for updated in updated_listings:
        listing_key = updated.get("ListingKey")
        if listing_key:
            listing_map[listing_key] = updated
    return list(listing_map.values())

def sync_listings(update_interval_hours: int = 12, purge: bool = False) -> list:
    """Sync listings by fetching updates and optionally purging stale data."""
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    # Load existing listings
    existing_listings = []
    if OUTPUT_FILE.exists():
        try:
            with OUTPUT_FILE.open("r", encoding="utf-8") as f:
                existing_listings = json.load(f)
            append_log({"event": "load_existing", "file": str(OUTPUT_FILE), "listing_count": len(existing_listings)})
        except Exception as e:
            append_log({"event": "read_error", "file": str(OUTPUT_FILE), "error": str(e)})
            print(f"⚠️ Failed to read {OUTPUT_FILE}: {e}")

    # Fetch updated listings
    listings = []
    try:
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(hours=update_interval_hours)
        listings = fetch_updated_listings(start_time, end_time)
        append_log({"event": "fetch_updated_complete", "updated_listings": len(listings)})
        print(f"✅ Fetched {len(listings)} updated listings")
    except Exception as e:
        append_log({"event": "fetch_updated_error", "error": str(e)})
        print(f"⚠️ Failed to fetch updated listings: {e}")

    # Merge updated listings with existing
    merged_listings = merge_listings(existing_listings, listings)

    # Purge stale listings if enabled
    if purge:
        try:
            accessible_keys = fetch_accessible_listing_keys()
            append_log({"event": "purge", "accessible_keys": len(accessible_keys)})
            merged_listings = [
                listing for listing in merged_listings
                if listing.get("ListingKey") in accessible_keys
            ]
            append_log({"event": "purge_complete", "purged_count": len(existing_listings) - len(merged_listings)})
            print(f"🗑️ Purged {len(existing_listings) - len(merged_listings)} stale listings")
        except Exception as e:
            append_log({"event": "purge_error", "error": str(e)})
            print(f"⚠️ Failed to purge stale listings: {e}")

    if not merged_listings:
        append_log({"event": "no_listings", "error": "No listings available to save"})
        print("⚠️ No listings to save, continuing")

    try:
        with OUTPUT_FILE.open("w", encoding="utf-8") as f:
            json.dump(merged_listings, f, indent=2)
        append_log({"event": "save_listings", "file": str(OUTPUT_FILE), "total_listings": len(merged_listings)})
        print(f"✅ Saved {len(merged_listings)} listings to {OUTPUT_FILE}")
    except Exception as e:
        append_log({"event": "write_error", "file": str(OUTPUT_FILE), "error": str(e)})
        raise Exception(f"❌ Failed to write to {OUTPUT_FILE}: {e}")

    return merged_listings

def fetch_all_listings() -> list:
    """Fetch all listings (fallback for initial run or if updates fail)."""
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    print(f"🚀 Fetching all listings with expansions: {', '.join(EXPANSIONS)}")
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }

    listings = []
    skiptoken = None
    page = 1
    batch_size = 500
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_expand={','.join(EXPANSIONS)}"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        append_log({"event": "fetch_all_listings", "page": page, "url": url})
        print(f"📄 Fetching all listings, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        append_log({"event": "fetch_all_listings_complete", "page": page, "listings_fetched": len(listings)})
                        print("✅ No more listings to fetch")
                        break
                    cleaned = [clean_data(item) for item in batch]
                    listings.extend(cleaned)
                    skiptoken = batch[-1].get("Id")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    append_log({"event": "rate_limited", "page": page, "wait_seconds": wait})
                    print(f"⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    append_log({"event": "fetch_error", "page": page, "status_code": res.status_code, "response": res.text})
                    raise Exception(f"❌ HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                append_log({"event": "fetch_error", "page": page, "error": str(e)})
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)

    return listings

if __name__ == "__main__":
    try:
        # Run sync with purge at 6 AM, without purge at 6 PM
        is_6am = datetime.now(UTC).hour == 6
        listings = sync_listings(update_interval_hours=12, purge=is_6am)
        if not listings:
            print("⚠️ No updated listings fetched, attempting full fetch")
            listings = fetch_all_listings()
            if listings:
                try:
                    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
                        json.dump(listings, f, indent=2)
                    append_log({"event": "save_full_fetch", "file": str(OUTPUT_FILE), "total_listings": len(listings)})
                    print(f"✅ Saved {len(listings)} listings from full fetch to {OUTPUT_FILE}")
                except Exception as e:
                    append_log({"event": "write_error", "file": str(OUTPUT_FILE), "error": str(e)})
                    raise Exception(f"❌ Failed to write to {OUTPUT_FILE}: {e}")
    except Exception as e:
        append_log({"event": "error", "error": str(e)})
        print(f"❌ Error in sync_listings.py: {e}")
        exit(1)