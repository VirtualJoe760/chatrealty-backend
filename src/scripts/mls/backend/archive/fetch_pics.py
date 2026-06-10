import os
import json
import time
import random
import requests
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load environment
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
SPARK_ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
BASE_URL = "https://replication.sparkapi.com/v1/listings"

if not MONGODB_URI or not SPARK_ACCESS_TOKEN:
    raise Exception("❌ Missing MONGODB_URI or SPARK_ACCESS_TOKEN")

client = MongoClient(MONGODB_URI)
db = client.get_database()
listings_collection = db.listings
photos_collection = db.photos

HEADERS = {
    "Authorization": f"Bearer {SPARK_ACCESS_TOKEN}",
    "Accept": "application/json"
}

# Config
MAX_WORKERS = 5
RETRY_LIMIT = 3
BATCH_SIZE = 100
DELAY_BETWEEN_BATCHES = 10  # seconds

# Paths
LOCAL_LOGS_DIR = Path(__file__).resolve().parents[4] / "local-logs"
LOCAL_LOGS_DIR.mkdir(exist_ok=True)
LOG_PATH = LOCAL_LOGS_DIR / "listing_photos.json"

def load_log():
    if LOG_PATH.exists():
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {entry["listingId"]: len(entry.get("photos", [])) for entry in data}
    return {}

def save_log(log_entries):
    log_list = [{"listingId": lid, "photos": count} for lid, count in log_entries.items()]
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log_list, f, indent=2)

def fetch_photos(listing_id: str):
    url = f"{BASE_URL}/{listing_id}/photos"

    for attempt in range(RETRY_LIMIT):
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code == 200:
                return res.json().get("D", {}).get("Results", [])
            elif res.status_code == 429:
                wait = [1, 3, 7][attempt]
                time.sleep(wait + random.uniform(0, 1))
            elif res.status_code == 403:
                return []
            else:
                return []
        except:
            time.sleep(1)
    return []

def save_photos_to_db(listing_id, photos):
    if not photos:
        return
    photos_collection.delete_many({"listingId": listing_id})
    ops = [
        ReplaceOne(
            {"listingId": listing_id, "photoId": photo["Id"]},
            {**photo, "listingId": listing_id},
            upsert=True
        )
        for photo in photos
    ]
    if ops:
        photos_collection.bulk_write(ops, ordered=False)

def chunked(iterable, size):
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]

def main():
    all_listings = list(listings_collection.find({"slug": {"$exists": True}}, {"slug": 1}))
    print(f"📦 Found {len(all_listings)} listings in local DB")

    existing_log = load_log()

    slugs_to_fetch = []
    for listing in all_listings:
        slug = listing.get("slug")
        if not slug:
            continue
        photo_count = photos_collection.count_documents({"listingId": slug})
        if slug not in existing_log or photo_count != existing_log[slug]:
            slugs_to_fetch.append(slug)

    print(f"🚀 Fetching {len(slugs_to_fetch)} listings missing or outdated in log")

    updated_log = dict(existing_log)
    batches = list(chunked(slugs_to_fetch, BATCH_SIZE))

    for batch_num, batch in enumerate(batches, start=1):
        print(f"📦 Batch {batch_num}/{len(batches)} ({len(batch)} listings)")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_photos, slug): slug for slug in batch}

            for future in as_completed(futures):
                slug = futures[future]
                photos = future.result()
                if photos:
                    save_photos_to_db(slug, photos)
                    updated_log[slug] = len(photos)
                else:
                    updated_log[slug] = 0  # Still log it

        print(f"✅ Batch {batch_num} complete — waiting {DELAY_BETWEEN_BATCHES}s...")
        time.sleep(DELAY_BETWEEN_BATCHES)

    save_log(updated_log)
    print(f"📝 Wrote updated photo counts to {LOG_PATH}")
    print("🏁 Done!")

if __name__ == "__main__":
    main()
