# src/scripts/mls/backend/patch.py
import os
import json
import time
from pathlib import Path
from pymongo import MongoClient
from pymongo.errors import (
    ConnectionFailure,
    ServerSelectionTimeoutError,
    BulkWriteError,
)
from dotenv import load_dotenv

# ---------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("❌ MONGODB_URI is not set in .env.local")

# ---------------------------------------------------------------------
# Connect to MongoDB
# ---------------------------------------------------------------------
print("🔌 Connecting to MongoDB...")
for attempt in range(3):
    try:
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,
            socketTimeoutMS=20000,
        )
        client.admin.command("ping")
        db = client.get_database()
        listings = db.listings
        temp = db.temp_flattened
        print("✅ Connected to MongoDB")
        break
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        if attempt == 2:
            raise
        print(f"⚠️ Connection attempt {attempt + 1} failed: {e}")
        time.sleep(2 ** attempt)

# ---------------------------------------------------------------------
# Load flattened JSON file
# ---------------------------------------------------------------------
input_path = (
    Path(__file__).resolve().parents[4]
    / "local-logs"
    / "flattened_all_listings_preserved.json"
)
if not input_path.exists():
    raise Exception(f"❌ File not found: {input_path}")

print(f"📄 Loading flattened listings from {input_path}")
with open(input_path, encoding="utf-8") as f:
    listings_data = json.load(f)
print(f"✅ Loaded {len(listings_data):,} listings from file")

# ---------------------------------------------------------------------
# Insert flattened data into temporary collection
# ---------------------------------------------------------------------
print("📥 Inserting flattened listings into temporary collection...")
temp.drop()
try:
    temp.insert_many(listings_data, ordered=False)
    print(f"✅ Inserted {len(listings_data):,} listings into temp_flattened")
except BulkWriteError as e:
    print(f"⚠️ Bulk write warning: {len(e.details.get('writeErrors', []))} duplicate inserts ignored")

# ---------------------------------------------------------------------
# Ensure indexes for the merge
# ---------------------------------------------------------------------
print("🧩 Ensuring indexes for $merge...")
listings.create_index("listingKey", unique=True)
temp.create_index("listingKey")
print("✅ Indexes confirmed on listingKey")

# ---------------------------------------------------------------------
# Run server-side $merge patch
# ---------------------------------------------------------------------
print("🔁 Running MongoDB $merge pipeline (server-side patch)...")

pipeline = [
    {
        "$merge": {
            "into": "listings",
            "on": "listingKey",
            "whenMatched": [
                {
                    "$set": {
                        "landType": "$landType",
                        "associationFee": "$associationFee",
                        "associationFeeFrequency": "$associationFeeFrequency",
                        "associationYN": "$associationYN",
                        "landLeaseAmount": "$landLeaseAmount",
                        "landLeasePer": "$landLeasePer",
                        "landLeaseExpirationDate": "$landLeaseExpirationDate",
                        "landLeaseYearsRemaining": "$landLeaseYearsRemaining",
                    }
                }
            ],
            "whenNotMatched": "discard",
        }
    }
]

start = time.time()
list(temp.aggregate(pipeline))
elapsed = round(time.time() - start, 2)
print(f"✅ Merge complete in {elapsed}s")

# ---------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------
temp.drop()
print("🧹 Dropped temporary collection temp_flattened")

# ---------------------------------------------------------------------
# Verification summary
# ---------------------------------------------------------------------
count_land = listings.count_documents({"landType": {"$exists": True}})
count_assoc = listings.count_documents({"associationFee": {"$exists": True}})
print(f"📊 Listings with landType: {count_land:,}")
print(f"📊 Listings with associationFee: {count_assoc:,}")
print("🏁 Patch operation finished successfully.")
