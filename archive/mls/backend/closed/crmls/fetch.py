import os
import json
import requests
import time
from pathlib import Path
from dotenv import load_dotenv

# Load env
env_path = Path(__file__).resolve().parents[6] / ".env.local"
load_dotenv(dotenv_path=env_path)

BASE_URL = "https://replication.sparkapi.com/v1/listings"
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
EXPANSIONS = ["Rooms", "Units", "OpenHouses", "VirtualTours"]
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[6] / "local-logs" / "closed" / "crmls"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOCAL_LOGS_DIR / "all_crmls_closed_listings_with_expansions.json"

def clean_data(obj):
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

def fetch_all_closed_listings():
    if not ACCESS_TOKEN:
        raise Exception("❌ SPARK_ACCESS_TOKEN is missing in .env.local")

    print(f"🚀 Fetching CRMLS CLOSED listings with expansions: {', '.join(EXPANSIONS)}")
    print(f"📋 Property Types: Residential (A), Residential Lease (B), Residential Income/Multi-Family (C)")
    print(f"🔒 Status: Closed (sold properties only)")
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
        # Filter for CRMLS MLS ID + property types + Closed status (sold properties)
        # CRMLS MLS ID from data share: 20200218121507636729000000
        mls_filter = "MlsId Eq '20200218121507636729000000'"
        property_filter = "PropertyType Eq 'A' Or PropertyType Eq 'B' Or PropertyType Eq 'C'"
        # Closed = actually sold (not pending, expired, or cancelled)
        status_filter = "StandardStatus Eq 'Closed'"
        combined_filter = f"{mls_filter} And ({property_filter}) And {status_filter}"

        url = f"{BASE_URL}?_limit={batch_size}&_expand={','.join(EXPANSIONS)}&_filter={combined_filter}"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        print(f"📄 Page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=headers, timeout=15)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        print("✅ No more CRMLS closed listings to fetch")
                        break
                    cleaned = [clean_data(item) for item in batch]
                    listings.extend(cleaned)
                    print(f"   📥 Fetched {len(batch)} closed listings (total: {len(listings)})")
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
        time.sleep(0.5)  # Slower throttle for large pulls

    if not listings:
        raise Exception("❌ No CRMLS closed listings fetched")

    try:
        with OUTPUT_FILE.open("w", encoding="utf-8") as f:
            json.dump(listings, f, indent=2)
        print(f"✅ Saved {len(listings)} CRMLS closed listings to {OUTPUT_FILE}")
    except Exception as e:
        raise Exception(f"❌ Failed to write to {OUTPUT_FILE}: {e}")

if __name__ == "__main__":
    try:
        fetch_all_closed_listings()
    except Exception as e:
        print(f"❌ Error in CRMLS closed fetch.py: {e}")
        exit(1)
