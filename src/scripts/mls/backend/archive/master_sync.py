
import os
import json
import time
import requests
import re
import unicodedata
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, ConnectionFailure, ServerSelectionTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, UTC
from typing import Dict, Any, Set, Optional, List

# ──────────────────────────────────────────────────────────────────────────────
# 🔧 ENV & CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Load .env.local
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")

if not ACCESS_TOKEN:
    raise ValueError("❌ Missing SPARK_ACCESS_TOKEN in .env.local")
if not MONGO_URI:
    raise ValueError("❌ Missing MONGODB_URI in .env.local")

BASE_URL = "https://replication.sparkapi.com/v1/listings"
EXPANSIONS = ["Rooms", "Units", "OpenHouses", "VirtualTours"]
LOG_DIR = Path(__file__).resolve().parents[4] / "local-logs"
PHOTO_LOG_DIR = LOG_DIR / "photo-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PHOTO_LOG_DIR.mkdir(parents=True, exist_ok=True)

LISTINGS_FILE = LOG_DIR / "all_listings_with_expansions.json"
FLATTENED_FILE = LOG_DIR / "flattened_all_listings_preserved.json"
SKIP_INDEX_PATH = PHOTO_LOG_DIR / "skip_index.json"
RUN_ID = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
RUN_LOG_PATH = PHOTO_LOG_DIR / f"run_{RUN_ID}.jsonl"

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

# ──────────────────────────────────────────────────────────────────────────────
# 🗃️ DB
# ──────────────────────────────────────────────────────────────────────────────

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, socketTimeoutMS=20000)
    client.admin.command("ping")
    db = client.get_database()
    listings_collection = db.listings
    photos_collection = db.photos
    print("✅ Connected to MongoDB")
except Exception as e:
    raise Exception(f"❌ Failed to connect to MongoDB: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# 🧾 Helpers
# ──────────────────────────────────────────────────────────────────────────────

def to_camel_case(s: str) -> str:
    parts = re.sub(r'(?<!^)(?=[A-Z])', '_', s).lower().split('_')
    return parts[0] + ''.join(word.capitalize() for word in parts[1:])

def simple_slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[\s]+", "-", value)

def extract_bool_keys(d: dict) -> str | None:
    if not isinstance(d, dict):
        return None
    keys = [k for k, v in d.items() if v is True]
    return ", ".join(keys) if keys else None

def camelize_keys(obj):
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if v in (None, "********", [], {}):
                continue
            camel_key = to_camel_case(k)
            if isinstance(v, dict) and all(isinstance(val, bool) for val in v.values()):
                flattened = extract_bool_keys(v)
                if flattened:
                    new_obj[camel_key] = flattened
            else:
                new_obj[camel_key] = camelize_keys(v)
        return new_obj
    elif isinstance(obj, list):
        return [camelize_keys(i) for i in obj]
    else:
        return obj

def append_run_log(entry: Dict[str, Any]) -> None:
    entry = {"ts": datetime.now(UTC).isoformat(), **camelize_keys(entry)}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def load_skip_index() -> Set[str]:
    if not SKIP_INDEX_PATH.exists():
        return set()
    try:
        with SKIP_INDEX_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "listingIds" in data:
                return set(str(x) for x in data["listingIds"])
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception as e:
        print(f"⚠️ Could not read skip index ({SKIP_INDEX_PATH}): {e}")
    return set()

def persist_skip_index(skip_ids: Set[str]) -> None:
    tmp_path = SKIP_INDEX_PATH.with_suffix(".json.tmp")
    payload = {"listingIds": sorted(list(skip_ids))}
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(SKIP_INDEX_PATH)

# ──────────────────────────────────────────────────────────────────────────────
# 📡 Sync Listings
# ──────────────────────────────────────────────────────────────────────────────

def fetch_updated_listings(start_time: datetime, end_time: datetime, batch_size: int = 500) -> List[Dict]:
    # Format timestamps without microseconds, ensuring UTC 'Z'
    start_str = start_time.replace(microsecond=0).isoformat() + "Z"
    end_str = end_time.replace(microsecond=0).isoformat() + "Z"
    time_filter = f"ModificationTimestamp bt {start_str},{end_str}"
    time_filter_encoded = urllib.parse.quote(time_filter)  # URL-encode the filter
    listings = []
    skiptoken = None
    page = 1
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_expand={','.join(EXPANSIONS)}&_filter={time_filter_encoded}"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        append_run_log({"event": "fetch_updated_listings", "page": page, "url": url})
        print(f"📄 Fetching updated listings, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=HEADERS, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        append_run_log({"event": "fetch_updated_listings_complete", "page": page, "listings_fetched": len(listings)})
                        break
                    cleaned = [camelize_keys(item) for item in batch]
                    listings.extend(cleaned)
                    skiptoken = batch[-1].get("Id")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    append_run_log({"event": "rate_limited", "page": page, "wait_seconds": wait})
                    time.sleep(wait)
                else:
                    append_run_log({"event": "fetch_error", "page": page, "status_code": res.status_code, "response": res.text})
                    raise Exception(f"HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                append_run_log({"event": "fetch_error", "page": page, "error": str(e)})
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)

    return listings

def fetch_accessible_listing_keys(batch_size: int = 1000) -> Set[str]:
    listing_keys = set()
    skiptoken = None
    page = 1
    retries = 3

    while True:
        url = f"{BASE_URL}?_limit={batch_size}&_select=ListingKey"
        if skiptoken:
            url += f"&_skiptoken={skiptoken}"

        append_run_log({"event": "fetch_listing_keys", "page": page, "url": url})
        print(f"📄 Fetching listing keys, page {page}: {url}")
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=HEADERS, timeout=10)
                if res.status_code == 200:
                    batch = res.json().get("D", {}).get("Results", [])
                    if not batch:
                        append_run_log({"event": "fetch_listing_keys_complete", "page": page, "keys_fetched": len(listing_keys)})
                        break
                    listing_keys.update(item.get("ListingKey") for item in batch if item.get("ListingKey"))
                    skiptoken = batch[-1].get("ListingKey")
                    break
                elif res.status_code == 429:
                    wait = 3 + attempt * 2
                    append_run_log({"event": "rate_limited", "page": page, "wait_seconds": wait})
                    time.sleep(wait)
                else:
                    append_run_log({"event": "fetch_error", "page": page, "status_code": res.status_code, "response": res.text})
                    raise Exception(f"HTTP {res.status_code}: {res.text}")
            except requests.RequestException as e:
                append_run_log({"event": "fetch_error", "page": page, "error": str(e)})
                if attempt == retries - 1:
                    raise Exception(f"❌ Max retries reached: {e}")
                time.sleep(2 ** attempt)

        if not batch:
            break

        page += 1
        time.sleep(0.3)

    return listing_keys

def merge_listings(existing_listings: List[Dict], updated_listings: List[Dict]) -> List[Dict]:
    listing_map = {listing.get("listingKey"): listing for listing in existing_listings if listing.get("listingKey")}
    for updated in updated_listings:
        listing_key = updated.get("listingKey")
        if listing_key:
            listing_map[listing_key] = updated
    return list(listing_map.values())

def sync_listings(update_interval_hours: int = 12, purge: bool = False) -> List[Dict]:
    existing_listings = []
    if LISTINGS_FILE.exists():
        try:
            with LISTINGS_FILE.open("r", encoding="utf-8") as f:
                existing_listings = json.load(f)
        except Exception as e:
            append_run_log({"event": "read_error", "file": str(LISTINGS_FILE), "error": str(e)})
            print(f"⚠️ Failed to read {LISTINGS_FILE}: {e}")

    try:
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(hours=update_interval_hours)
        updated_listings = fetch_updated_listings(start_time, end_time)
        append_run_log({"event": "sync_updated", "updated_listings": len(updated_listings)})
    except Exception as e:
        append_run_log({"event": "sync_error", "error": str(e)})
        print(f"⚠️ Failed to fetch updated listings: {e}")
        updated_listings = []

    merged_listings = merge_listings(existing_listings, updated_listings)

    if purge:
        try:
            accessible_keys = fetch_accessible_listing_keys()
            append_run_log({"event": "purge", "accessible_keys": len(accessible_keys)})
            merged_listings = [
                listing for listing in merged_listings
                if listing.get("listingKey") in accessible_keys
            ]
            append_run_log({"event": "purge_complete", "purged_count": len(existing_listings) - len(merged_listings)})
        except Exception as e:
            append_run_log({"event": "purge_error", "error": str(e)})
            print(f"⚠️ Failed to purge stale listings: {e}")

    try:
        with LISTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(merged_listings, f, indent=2)
        append_run_log({"event": "save_listings", "file": str(LISTINGS_FILE), "total_listings": len(merged_listings)})
    except Exception as e:
        append_run_log({"event": "write_error", "file": str(LISTINGS_FILE), "error": str(e)})
        raise Exception(f"❌ Failed to write to {LISTINGS_FILE}: {e}")

    return merged_listings

# ──────────────────────────────────────────────────────────────────────────────
# 📄 Flatten Listings
# ──────────────────────────────────────────────────────────────────────────────

def flatten_listing(raw: Dict) -> Dict | None:
    standard = raw.get("standardFields", {})
    listing_key = standard.get("listingKey")
    if not listing_key:
        append_run_log({"event": "flatten_skipped", "reason": "missing_listing_key", "id": raw.get("id", "unknown")})
        return None

    output = {}
    output.update(camelize_keys(standard))

    for key, value in raw.items():
        if key == "standardFields" or value in (None, "********", [], {}):
            continue
        camel_key = to_camel_case(key)
        if camel_key not in output:
            output[camel_key] = camelize_keys(value)

    unparsed = standard.get("unparsedAddress") or raw.get("unparsedAddress")
    slug_address = simple_slugify(unparsed) if unparsed else "unknown"

    final = {
        "slug": listing_key,
        "slugAddress": slug_address,
        "listingId": listing_key,  # Ensure listingId is set for seeding
    }
    final.update(output)
    return final

def flatten_listings(listings: List[Dict]) -> List[Dict]:
    flattened = []
    skipped = 0

    for item in listings:
        flat = flatten_listing(item)
        if flat:
            flattened.append(flat)
        else:
            skipped += 1

    append_run_log({"event": "flatten_complete", "flattened_count": len(flattened), "skipped_count": skipped})
    try:
        with FLATTENED_FILE.open("w", encoding="utf-8") as f:
            json.dump(flattened, f, indent=2)
        append_run_log({"event": "save_flattened", "file": str(FLATTENED_FILE), "total_listings": len(flattened)})
    except Exception as e:
        append_run_log({"event": "write_error", "file": str(FLATTENED_FILE), "error": str(e)})
        raise Exception(f"❌ Failed to write to {FLATTENED_FILE}: {e}")

    return flattened

# ──────────────────────────────────────────────────────────────────────────────
# 🌱 Seed MongoDB
# ──────────────────────────────────────────────────────────────────────────────

def seed_listings(flattened_listings: List[Dict]) -> None:
    operations = []
    skipped = 0

    for raw in flattened_listings:
        listing_id = raw.get("listingId")
        address = raw.get("unparsedAddress")
        slug = raw.get("slug")

        if not listing_id or not address or not slug:
            append_run_log({"event": "seed_skipped", "reason": "missing_required_fields", "id": listing_id or "unknown"})
            skipped += 1
            continue

        raw["slugAddress"] = simple_slugify(address)
        raw.pop("_id", None)

        operations.append(
            UpdateOne(
                {"listingId": listing_id},
                {"$set": raw},
                upsert=True,
            )
        )

    if not operations:
        append_run_log({"event": "seed_error", "reason": "no_valid_operations"})
        print("⚠️ No valid listings to seed, continuing with photo caching")
        return

    batch_size = 500
    updated = 0
    failed = 0

    for i in range(0, len(operations), batch_size):
        chunk = operations[i:i + batch_size]
        try:
            result = listings_collection.bulk_write(chunk, ordered=False)
            modified = result.modified_count or 0
            upserted = result.upserted_count or 0
            updated += modified + upserted
            append_run_log({
                "event": "seed_batch",
                "batch_number": i // batch_size + 1,
                "modified_count": modified,
                "upserted_count": upserted
            })
        except BulkWriteError as e:
            batch_errors = len(e.details.get("writeErrors", [])) if e.details else 1
            failed += batch_errors
            append_run_log({
                "event": "seed_batch_error",
                "batch_number": i // batch_size + 1,
                "error_count": batch_errors
            })
        except Exception as e:
            append_run_log({
                "event": "seed_batch_error",
                "batch_number": i // batch_size + 1,
                "error": str(e)
            })
            raise Exception(f"❌ Batch {i // batch_size + 1} failed: {e}")

    append_run_log({
        "event": "seed_complete",
        "updated_count": updated,
        "skipped_count": skipped,
        "failed_count": failed
    })
    if failed > 0:
        print(f"⚠️ {failed} seeding operations failed, continuing with photo caching")

# ──────────────────────────────────────────────────────────────────────────────
# 📸 Cache Photos
# ──────────────────────────────────────────────────────────────────────────────

def fetch_listing_photos(slug: str, retries: int = 3):
    url = f"{BASE_URL}/{slug}/photos"
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                return res.json().get("D", {}).get("Results", [])
            elif res.status_code == 403:
                return {"_403": True, "body": res.text}
            elif res.status_code == 429 or "over rate" in res.text.lower():
                wait = 3 + attempt * 2
                append_run_log({"event": "rate_limited", "slug": slug, "wait_seconds": wait})
                time.sleep(wait)
            else:
                append_run_log({"event": "fetch_photo_error", "slug": slug, "status_code": res.status_code, "response": res.text})
                raise Exception(f"HTTP {res.status_code}: {res.text}")
        except requests.RequestException as e:
            append_run_log({"event": "fetch_photo_error", "slug": slug, "error": str(e)})
            if attempt == retries - 1:
                raise Exception(f"❌ Max retries reached for {slug}: {e}")
            time.sleep(2 ** attempt)
    raise Exception(f"❌ Max retries reached for {slug}")

def cache_photo_for_listing(listing: Dict[str, Any], skip_ids: Set[str]) -> str:
    slug = listing.get("slug")
    listing_id = str(listing.get("listingId")) if listing.get("listingId") else None

    if not slug or not listing_id:
        append_run_log({"event": "photo_skipped", "reason": "missing_required_fields", "listingId": listing_id or "unknown"})
        return f"⚠️ Skipped: missing slug or listingId for {listing.get('_id', 'unknown')}"

    if listing_id in skip_ids:
        return f"⏭️ Pre-skipped {slug} (in skip_index)"

    if photos_collection.find_one({"listingId": listing_id}):
        append_run_log({"event": "photo_skipped", "reason": "already_cached", "listingId": listing_id, "slug": slug})
        return f"⏩ Skipped {slug} (already cached)"

    try:
        photos = fetch_listing_photos(slug)
        if isinstance(photos, dict) and photos.get("_403"):
            skip_ids.add(listing_id)
            append_run_log({
                "event": "photo_skipped",
                "reason": "permission_denied",
                "listingId": listing_id,
                "slug": slug,
                "response": photos["body"]
            })
            persist_skip_index(skip_ids)
            return f"🚫 Permission denied for {slug} (skipped permanently)"

        if not photos:
            skip_ids.add(listing_id)
            append_run_log({"event": "photo_skipped", "reason": "no_photos", "listingId": listing_id, "slug": slug})
            persist_skip_index(skip_ids)
            return f"⚠️ No photos for {slug}"

        primary = photos[0]
        doc = {
            "listingId": listing_id,
            "photoId": primary.get("Id"),
            "caption": primary.get("Caption"),
            "uriThumb": primary.get("UriThumb"),
            "uri300": primary.get("Uri300"),
            "uri640": primary.get("Uri640"),
            "uri800": primary.get("Uri800"),
            "uri1024": primary.get("Uri1024"),
            "uri1280": primary.get("Uri1280"),
            "uri1600": primary.get("Uri1600"),
            "uri2048": primary.get("Uri2048"),
            "uriLarge": primary.get("UriLarge"),
            "primary": primary.get("Primary", True),
        }

        if not doc["photoId"]:
            skip_ids.add(listing_id)
            append_run_log({"event": "photo_skipped", "reason": "no_photo_id", "listingId": listing_id, "slug": slug})
            persist_skip_index(skip_ids)
            return f"⚠️ No valid photoId for {slug}"

        photos_collection.update_one({"photoId": doc["photoId"]}, {"$set": doc}, upsert=True)
        append_run_log({
            "event": "photo_cached",
            "listingId": listing_id,
            "slug": slug,
            "photoId": doc["photoId"]
        })
        time.sleep(0.5)
        return f"✅ Cached photo for {slug}"
    except Exception as e:
        append_run_log({
            "event": "photo_error",
            "listingId": listing_id,
            "slug": slug,
            "error": str(e)
        })
        return f"❌ Failed for {slug}: {e}"

def cache_photos(flattened_listings: List[Dict]) -> None:
    skip_ids = load_skip_index()
    append_run_log({"event": "photo_cache_start", "skip_index_size": len(skip_ids)})

    listings = [l for l in flattened_listings if l.get("listingId") and str(l["listingId"]) not in skip_ids]
    append_run_log({"event": "photo_cache_filtered", "remaining_listings": len(listings)})

    failed = 0
    processed = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(cache_photo_for_listing, l, skip_ids) for l in listings]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                failed += 1
                append_run_log({"event": "photo_worker_error", "error": str(e)})
                continue
            processed += 1
            if result and "❌" in result:
                failed += 1
            print(result)

    append_run_log({
        "event": "photo_cache_complete",
        "processed_count": processed,
        "failed_count": failed,
        "skip_index_size": len(skip_ids)
    })

# ──────────────────────────────────────────────────────────────────────────────
# 🚀 Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    append_run_log({"event": "start", "run_id": RUN_ID})
    try:
        # Step 1: Sync listings (fetch updates, purge stale data daily)
        is_6am = datetime.now(UTC).hour == 6
        listings = sync_listings(update_interval_hours=12, purge=is_6am)

        # Step 2: Flatten listings
        flattened_listings = flatten_listings(listings)

        # Step 3: Seed MongoDB
        seed_listings(flattened_listings)

        # Step 4: Cache photos
        cache_photos(flattened_listings)

        append_run_log({"event": "complete", "run_id": RUN_ID})
        print("🏁 Master sync complete")
    except Exception as e:
        append_run_log({"event": "error", "error": str(e)})
        print(f"❌ Unhandled error in master_sync.py: {e}")
        exit(1)

if __name__ == "__main__":
    main()

# ──────────────────────────────────────────────────────────────────────────────
# ⏰ Task Schedule
# ──────────────────────────────────────────────────────────────────────────────

# Here's the Task Schedule for running the master sync twice daily at 6 AM and 6 PM:

{
  "name": "Master Listings Sync",
  "prompt": "Run the master_sync.py script to fetch updated listings, purge stale data, flatten listings, seed MongoDB, and cache primary photos.",
  "cadence": "daily",
  "time_of_day": "06:00",
  "day_of_week": 1,
  "day_of_month": 1,
  "day_of_year": 1
}

{
  "name": "Master Listings Sync",
  "prompt": "Run the master_sync.py script to fetch updated listings, purge stale data, flatten listings, seed MongoDB, and cache primary photos.",
  "cadence": "daily",
  "time_of_day": "18:00",
  "day_of_week": 1,
  "day_of_month": 1,
  "day_of_year": 1
}
