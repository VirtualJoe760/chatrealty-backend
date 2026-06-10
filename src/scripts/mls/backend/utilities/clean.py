import os
from pathlib import Path
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI is not set in .env.local")

# Connect to MongoDB
client = MongoClient(MONGODB_URI)
db = client.get_database()
collection = db.listings

def remove_pascal_case_keys():
    cursor = collection.find({}, {"_id": 1})  # Only get _id to reduce transfer
    total_updated = 0

    for doc in cursor:
        _id = doc["_id"]
        full_doc = collection.find_one({"_id": _id})
        pascal_keys = {k: "" for k in full_doc.keys() if k[:1].isupper()}

        if pascal_keys:
            result = collection.update_one({"_id": _id}, {"$unset": pascal_keys})
            if result.modified_count > 0:
                total_updated += 1
                print(f"✅ Cleaned {_id} - Removed: {list(pascal_keys.keys())}")

    print(f"\n🏁 Done. Cleaned {total_updated} documents.")

if __name__ == "__main__":
    remove_pascal_case_keys()
