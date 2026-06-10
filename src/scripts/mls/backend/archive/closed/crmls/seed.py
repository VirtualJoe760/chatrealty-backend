# src/scripts/mls/backend/closed/crmls/seed.py

import os
import json
import time
from pathlib import Path
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, ConnectionFailure, ServerSelectionTimeoutError
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[6] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("❌ MONGODB_URI is not set in .env.local")

# Connect to MongoDB with retry
retries = 3
for attempt in range(retries):
    try:
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,
            socketTimeoutMS=20000,
        )
        # Force a server selection now (otherwise it's lazy)
        client.admin.command("ping")
        db = client.get_database()
        collection = db.crmlsClosedListings  # Separate collection for CRMLS closed listings
        print("✅ Connected to MongoDB - crmlsClosedListings collection")
        break
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        if attempt == retries - 1:
            raise Exception(f"❌ Failed to connect to MongoDB after {retries} attempts: {e}")
        print(f"⚠️ MongoDB connection attempt {attempt + 1} failed, retrying...")
        time.sleep(2 ** attempt)

# File to load listings from
INPUT_FILE = Path(__file__).resolve().parents[6] / "local-logs" / "closed" / "crmls" / "flattened_crmls_closed_listings.json"

# Utilities
def simple_slugify(s: str) -> str:
    return (
        s.lower()
        .replace(",", "")
        .replace(".", "")
        .replace("/", "-")
        .replace(" ", "-")
        .strip()
    )

def seed():
    if not INPUT_FILE.exists():
        raise Exception(f"❌ Input file {INPUT_FILE} does not exist")

    print(f"📄 Loading flattened CRMLS closed listings from {INPUT_FILE}")
    try:
        with open(INPUT_FILE, encoding="utf-8") as f:
            listings = json.load(f)
    except Exception as e:
        raise Exception(f"❌ Failed to read {INPUT_FILE}: {e}")

    operations = []
    skipped = 0

    for raw in listings:
        listing_id = raw.get("listingId")
        address = raw.get("unparsedAddress")
        slug = raw.get("slug") or listing_id

        if not listing_id or not address or not slug:
            print(f"⚠️ Skipping listing: missing required fields: {raw.get('id') or '[no id]'}")
            skipped += 1
            continue

        # Normalize fields we rely on
        raw["listingId"] = listing_id
        raw["slug"] = slug
        raw["slugAddress"] = simple_slugify(address)

        # Remove Mongo _id if present to avoid duplicate key errors on upsert
        raw.pop("_id", None)

        operations.append(
            UpdateOne(
                {"listingId": listing_id},
                {"$set": raw},
                upsert=True,
            )
        )

    if not operations:
        raise Exception("❌ No valid CRMLS closed listings to update")

    print(f"🔁 Updating {len(operations)} CRMLS closed listings in batches...")
    batch_size = 500
    updated = 0
    failed = 0

    for i in range(0, len(operations), batch_size):
        chunk = operations[i : i + batch_size]
        try:
            result = collection.bulk_write(chunk, ordered=False)
            modified = result.modified_count or 0
            upserted = result.upserted_count or 0
            updated += modified + upserted
            print(f"✅ Batch {i // batch_size + 1}: Modified {modified}, Upserted {upserted}")
        except BulkWriteError as e:
            batch_errors = len(e.details.get("writeErrors", [])) if e.details else 1
            failed += batch_errors
            print(f"⚠️ Batch {i // batch_size + 1} failed with {batch_errors} errors")
        except Exception as e:
            raise Exception(f"❌ Batch {i // batch_size + 1} failed: {e}")

    print(f"🏁 Complete: Updated {updated} CRMLS closed listings. Skipped: {skipped}, Failed: {failed}")
    if failed > 0:
        raise Exception(f"❌ {failed} operations failed during seeding")

if __name__ == "__main__":
    try:
        seed()
    except Exception as e:
        print(f"❌ Error in CRMLS closed seed.py: {e}")
        exit(1)
