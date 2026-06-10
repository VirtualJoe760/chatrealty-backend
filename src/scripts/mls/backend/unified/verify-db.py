#!/usr/bin/env python3
"""
Verify Unified Listings MongoDB Collection

Checks data integrity, index creation, and MLS distribution after seeding.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables
env_path = Path(__file__).resolve().parents[5] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
COLLECTION_NAME = "unified_listings"


def verify_collection():
    """Verify unified_listings collection data and indexes"""

    if not MONGODB_URI:
        raise Exception("[ERROR] MONGODB_URI is missing in .env.local")

    # Connect to MongoDB
    client = MongoClient(MONGODB_URI)
    db = client.get_database()
    collection = db[COLLECTION_NAME]

    print("=" * 80)
    print("Unified Listings Collection Verification")
    print("=" * 80)
    print(f"[OK] Connected to MongoDB")
    print(f"[OK] Using collection: {COLLECTION_NAME}\n")

    # 1. Total count
    total_count = collection.count_documents({})
    print(f">>> Total listings in collection: {total_count:,}\n")

    # 2. Count by MLS source
    print(">>> Count by MLS Source:")
    print("-" * 80)
    pipeline = [
        {"$group": {"_id": "$mlsSource", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    mls_counts = list(collection.aggregate(pipeline))

    for mls in mls_counts:
        mls_name = mls["_id"] or "UNKNOWN"
        count = mls["count"]
        percentage = (count / total_count * 100) if total_count > 0 else 0
        print(f"  {mls_name:25} {count:>8,} ({percentage:>5.1f}%)")

    print()

    # 3. Count by StandardStatus
    print(">>> Count by StandardStatus:")
    print("-" * 80)
    pipeline = [
        {"$group": {"_id": "$standardStatus", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    status_counts = list(collection.aggregate(pipeline))

    for status in status_counts:
        status_name = status["_id"] or "UNKNOWN"
        count = status["count"]
        percentage = (count / total_count * 100) if total_count > 0 else 0
        print(f"  {status_name:25} {count:>8,} ({percentage:>5.1f}%)")

    print()

    # 4. Count by PropertyType
    print(">>> Count by PropertyType:")
    print("-" * 80)
    pipeline = [
        {"$group": {"_id": "$propertyType", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    type_counts = list(collection.aggregate(pipeline))

    for ptype in type_counts:
        type_code = ptype["_id"] or "UNKNOWN"
        count = ptype["count"]
        percentage = (count / total_count * 100) if total_count > 0 else 0
        print(f"  {type_code:25} {count:>8,} ({percentage:>5.1f}%)")

    print()

    # 5. Count by PropertyTypeName
    print(">>> Count by PropertyTypeName:")
    print("-" * 80)
    pipeline = [
        {"$group": {"_id": "$propertyTypeName", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    type_name_counts = list(collection.aggregate(pipeline))

    for ptype in type_name_counts:
        type_name = ptype["_id"] or "UNKNOWN"
        count = ptype["count"]
        percentage = (count / total_count * 100) if total_count > 0 else 0
        print(f"  {type_name:25} {count:>8,} ({percentage:>5.1f}%)")

    print()

    # 6. Geospatial data check
    with_coords = collection.count_documents({"coordinates": {"$exists": True}})
    without_coords = total_count - with_coords
    coords_percentage = (with_coords / total_count * 100) if total_count > 0 else 0

    print(">>> Geospatial Coordinates:")
    print("-" * 80)
    print(f"  With coordinates:        {with_coords:>8,} ({coords_percentage:>5.1f}%)")
    print(f"  Without coordinates:     {without_coords:>8,}")
    print()

    # 7. Index verification
    print(">>> Indexes:")
    print("-" * 80)
    indexes = collection.list_indexes()
    for idx in indexes:
        idx_name = idx.get("name", "unknown")
        idx_keys = idx.get("key", {})
        idx_type = "2dsphere" if "2dsphere" in str(idx_keys.values()) else "standard"
        unique = " (UNIQUE)" if idx.get("unique") else ""
        print(f"  [{idx_type:>10}] {idx_name}{unique}")

    print()

    # 8. Sample listing
    print(">>> Sample Listing (first record):")
    print("-" * 80)
    sample = collection.find_one({})
    if sample:
        print(f"  ListingKey:       {sample.get('listingKey', 'N/A')}")
        print(f"  MLS Source:       {sample.get('mlsSource', 'N/A')}")
        print(f"  MLS ID:           {sample.get('mlsId', 'N/A')}")
        print(f"  PropertyType:     {sample.get('propertyType', 'N/A')}")
        print(f"  PropertyTypeName: {sample.get('propertyTypeName', 'N/A')}")
        print(f"  StandardStatus:   {sample.get('standardStatus', 'N/A')}")
        print(f"  City:             {sample.get('city', 'N/A')}")
        print(f"  List Price:       ${sample.get('listPrice', 0):,}")
        print(f"  Coordinates:      {sample.get('coordinates', 'N/A')}")

    print()
    print("=" * 80)
    print("Verification Complete")
    print("=" * 80)

    client.close()


if __name__ == "__main__":
    try:
        verify_collection()
    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        sys.exit(1)
