import os
import json
import requests
import time
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
if not ACCESS_TOKEN:
    raise Exception("❌ Missing SPARK_ACCESS_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

BASE_URL = "https://replication.sparkapi.com/v1"
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[4] / "local-logs"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
CRMLS_ID = "20200218121507636729000000"
OUTPUT_FILE = LOCAL_LOGS_DIR / "crmls_all_listings.json"

def get_total_count():
    print(f"🔍 Counting total listings in CRMLS (MLS ID: {CRMLS_ID})...")
    url = f"{BASE_URL}/listings?_filter=MlsId eq '{CRMLS_ID}'&_count=true&_limit=1"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200:
        print(f"❌ Failed to get count: {res.status_code} – {res.text}")
        return 0
    return res.json().get("D", {}).get("Count", 0)

def fetch_all_listings():
    total = get_total_count()
    print(f"📦 Total listings to fetch: {total}\n")

    listings = []
    skiptoken = None
    page = 1

    while True:
        url = f"{BASE_URL}/listings?_limit=1000&_filter=MlsId eq '{CRMLS_ID}'"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        print(f"📄 Fetching page {page}...")
        res = requests.get(url, headers=HEADERS)
        if res.status_code != 200:
            print(f"❌ Error on page {page}: {res.status_code} – {res.text}")
            break

        batch = res.json().get("D", {}).get("Results", [])
        if not batch:
            break

        listings.extend(batch)
        skiptoken = batch[-1].get("Id")
        page += 1
        time.sleep(0.2)  # Be gentle to the API

    print(f"\n✅ Fetched {len(listings)} total listings.")
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(listings, f, indent=2)
    print(f"📁 Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    fetch_all_listings()
