# src/scripts/mls/backend/count_target.py

import os
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
BASE_URL = "https://replication.sparkapi.com/v1/listings"

def count_total_listings():
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }

    url = f"{BASE_URL}?_pagination=count"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"❌ Error: {response.text}")
        return

    data = response.json()
    total_rows = data.get("D", {}).get("Pagination", {}).get("TotalRows", "Unknown")
    
    print(f"📊 Total listings available via Spark API: {total_rows}")

if __name__ == "__main__":
    count_total_listings()
