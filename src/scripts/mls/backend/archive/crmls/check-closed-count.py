import os
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load .env.local
env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
BASE_URL = "https://replication.sparkapi.com/v1/listings"

# MLS IDs
GPS_MLS_ID = "20200615204919817905000000"       # GPS Primary MLS
CRMLS_MLS_ID = "20200218121507636729000000"     # CRMLS (data share)

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

def get_count_for_mls(mls_id: str):
    """Returns the total listing count for a given MLS."""
    url = f"{BASE_URL}?_filter=MlsId eq '{mls_id}'&_pagination=count"

    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()

        # Get the count
        return data["D"]["Pagination"]["TotalRows"]

    except Exception as e:
        print(f"❌ Error fetching MLS {mls_id}: {e}")
        print("Response:", res.text if 'res' in locals() else "No response")
        return None


if __name__ == "__main__":
    print("📡 Counting listings for GPS + CRMLS...")

    gps_count = get_count_for_mls(GPS_MLS_ID)
    crmls_count = get_count_for_mls(CRMLS_MLS_ID)

    print("\n============================= RESULTS =============================")
    print(f"📍 GPS MLS ({GPS_MLS_ID}):       {gps_count:,} listings")
    print(f"📍 CRMLS MLS ({CRMLS_MLS_ID}):   {crmls_count:,} listings")
    print("-------------------------------------------------------------------")

    if gps_count is not None and crmls_count is not None:
        print(f"📊 Combined Total:               {gps_count + crmls_count:,} listings")
    print("===================================================================")
