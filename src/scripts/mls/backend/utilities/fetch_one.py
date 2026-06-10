# src/scripts/mls/backend/fetch_one.py

import os
import json
import requests
import time
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

SPARK_ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
BASE_URL = "https://replication.sparkapi.com/v1/listings"
HEADERS = {
    "Authorization": f"Bearer {SPARK_ACCESS_TOKEN}",
    "Accept": "application/json"
}

EXPANSIONS = ["Rooms", "Units", "OpenHouses", "VirtualTours"]
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[4] / "src/local-logs"
LOCAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOCAL_LOGS_DIR / "single_listing.json"

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

def fetch_one_listing():
    url = f"{BASE_URL}?_limit=1&_expand={','.join(EXPANSIONS)}"
    print(f"🚀 Fetching one listing with expansions: {url}")

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        print(f"❌ Error: {response.text}")
        return

    data = response.json().get("D", {}).get("Results", [])
    if not data:
        print("❌ No listings returned.")
        return

    cleaned = clean_data(data[0])

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)

    print(f"✅ Saved 1 listing to {OUTPUT_FILE}")

if __name__ == "__main__":
    fetch_one_listing()
