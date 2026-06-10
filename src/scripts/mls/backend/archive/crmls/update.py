# src/scripts/mls/backend/crmls/update.py
# CRMLS Listing Status Update Script

import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV & CONFIG
# ──────────────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")
BASE_URL = "https://replication.sparkapi.com/v1/listings"

if not ACCESS_TOKEN or not MONGO_URI:
    raise ValueError("❌ Missing SPARK_ACCESS_TOKEN or MONGODB_URI in .env.local")

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

LOG_DIR = Path(__file__).resolve().parents[5] / "local-logs" / "crmls" / "status-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DATABASE
# ──────────────────────────────────────────────────────────────────────────────

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()
collection = db.crmls_listings  # CRMLS active collection
closed_collection = db.crmlsClosedListings  # CRMLS closed/sold listings
print("✅ Connected to MongoDB (CRMLS active + closed collections)")

# ──────────────────────────────────────────────────────────────────────────────
# 🌐 FETCH SINGLE LISTING STATUS
# ──────────────────────────────────────────────────────────────────────────────

def fetch_listing_status(listing_key: str):
    """Query Spark for current StandardStatus + StatusChangeTimestamp for one listing."""
    url = f"{BASE_URL}/{listing_key}?_select=ListingKey,StandardStatus,StatusChangeTimestamp"

    for attempt in range(3):
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            if res.status_code == 200:
                data = res.json().get("D", {}).get("Results", [])
                if data:
                    return data[0].get("StandardFields", {})
                return None
            elif res.status_code in (403, 404):
                return None  # Listing removed/off-market
            elif res.status_code == 429:
                wait = 3 + attempt * 2  # short backoff
                print(f"⏳ Rate limited on {listing_key}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"⚠️ HTTP {res.status_code}: {res.text[:120]}")
        except requests.RequestException as e:
            print(f"⚠️ Network error for {listing_key}: {e}")
        time.sleep(2 + attempt)
    return None

# ──────────────────────────────────────────────────────────────────────────────
# 🔁 CHECK & UPDATE SINGLE LISTING
# ──────────────────────────────────────────────────────────────────────────────

def check_listing(listing):
    """Compare Spark vs local StatusChangeTimestamp and update if newer."""
    listing_key = str(listing.get("listingKey") or listing.get("slug"))
    if not listing_key or len(listing_key) < 20:
        return f"⚠️ Skipping invalid key: {listing_key}"

    local_status = listing.get("standardStatus")
    local_ts = listing.get("statusChangeTimestamp")

    spark = fetch_listing_status(listing_key)
    if not spark:
        collection.update_one(
            {"listingKey": listing_key},
            {"$set": {
                "standardStatus": "OffMarket",
                "statusLastChecked": datetime.now(timezone.utc).isoformat(),
            }},
        )
        return f"❌ {listing_key} appears OffMarket or removed"

    spark_status = spark.get("StandardStatus")
    spark_ts = spark.get("StatusChangeTimestamp")

    if not spark_ts or not local_ts:
        if spark_status != local_status:
            # Check if listing is now CLOSED (sold) - move to closed collection
            if spark_status == "Closed":
                full_listing = collection.find_one({"listingKey": listing_key})
                if full_listing:
                    full_listing["standardStatus"] = spark_status
                    full_listing["statusLastChecked"] = datetime.now(timezone.utc).isoformat()
                    full_listing.pop("_id", None)  # Remove _id to avoid duplicate key error
                    closed_collection.update_one(
                        {"listingKey": listing_key},
                        {"$set": full_listing},
                        upsert=True
                    )
                    collection.delete_one({"listingKey": listing_key})
                    return f"🏠💰 {listing_key}: {local_status} → SOLD (moved to closed collection)"
            else:
                # Other status changes (Pending, Expired, etc.) - just update in place
                collection.update_one(
                    {"listingKey": listing_key},
                    {"$set": {
                        "standardStatus": spark_status,
                        "statusLastChecked": datetime.now(timezone.utc).isoformat(),
                    }},
                )
                return f"🔄 {listing_key}: {local_status} → {spark_status}"
        return f"✅ {listing_key}: unchanged"

    try:
        spark_dt = datetime.fromisoformat(spark_ts.replace("Z", "+00:00"))
        local_dt = datetime.fromisoformat(local_ts.replace("Z", "+00:00"))
    except Exception:
        spark_dt = datetime.now(timezone.utc)
        local_dt = datetime.now(timezone.utc)

    if spark_dt > local_dt or spark_status != local_status:
        # Check if listing is now CLOSED (sold) - move to closed collection
        if spark_status == "Closed":
            full_listing = collection.find_one({"listingKey": listing_key})
            if full_listing:
                full_listing["standardStatus"] = spark_status
                full_listing["statusChangeTimestamp"] = spark_ts
                full_listing["statusLastChecked"] = datetime.now(timezone.utc).isoformat()
                full_listing.pop("_id", None)  # Remove _id to avoid duplicate key error
                closed_collection.update_one(
                    {"listingKey": listing_key},
                    {"$set": full_listing},
                    upsert=True
                )
                collection.delete_one({"listingKey": listing_key})
                return f"🏠💰 {listing_key}: {local_status} → SOLD (moved to closed collection)"
        else:
            # Other status changes (Pending, Expired, etc.) - just update in place
            collection.update_one(
                {"listingKey": listing_key},
                {"$set": {
                    "standardStatus": spark_status,
                    "statusChangeTimestamp": spark_ts,
                    "statusLastChecked": datetime.now(timezone.utc).isoformat(),
                }},
            )
            return f"🔁 {listing_key}: status updated → {spark_status}"
    else:
        return f"✅ {listing_key}: unchanged"

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 MAIN EXECUTION WITH BATCH REST
# ──────────────────────────────────────────────────────────────────────────────

def main():
    active_statuses = ["Active", "Pending", "Hold", "ComingSoon"]
    query = {"standardStatus": {"$in": active_statuses}}
    listings = list(collection.find(query, {
        "listingKey": 1, "slug": 1, "standardStatus": 1, "statusChangeTimestamp": 1
    }))
    total = len(listings)
    print(f"🔍 Checking Spark status for {total} CRMLS listings ({', '.join(active_statuses)})")

    changed = 0
    removed = 0
    sold = 0  # Track listings moved to closed collection
    checked = 0
    batch_size = 1000

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_listing, l): l for l in listings}

        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                checked += 1
                if "🏠💰" in result:
                    sold += 1
                elif "🔄" in result or "🔁" in result:
                    changed += 1
                elif "❌" in result:
                    removed += 1
                print(f"[{i}/{total}] {result}")
                time.sleep(0.18)  # micro-throttle
            except Exception as e:
                print(f"❌ Worker error: {e}")

            # ─── BATCH PAUSE ─────────────────────────────────────────────────────
            if i % batch_size == 0:
                print(f"😴 Processed {i} listings — resting for 60 seconds to avoid rate limits...")
                time.sleep(60)
                print("✅ Resuming updates...\n")

    # Summary
    print(f"\n🏁 Done! {changed} CRMLS listings updated, {sold} sold (moved to closed), {removed} off-market, checked {checked} total.")
    log = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checked": checked,
        "changed": changed,
        "sold": sold,
        "removed": removed,
    }
    log_path = LOG_DIR / f"status_check_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    print(f"🪵 Log saved → {log_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user, exiting gracefully.")
    except Exception as e:
        print(f"❌ Unhandled error: {e}")
