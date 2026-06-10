#!/usr/bin/env python3
"""
Unified MLS Seed Script - CLOSED LISTINGS

Seeds the unified_closed_listings MongoDB collection with closed sales from all 8 MLSs.
Based on seed.py but modified for closed listings collection.

Features:
- Bulk upsert operations (500 per batch)
- Geospatial indexing (for CMA radius queries)
- Compound indexes (for appreciation analysis, CMA filtering)
- TTL index (auto-delete sales older than 5 years)
- Automatic index creation
- Progress tracking

Usage:
    # Seed all MLS files at once
    python src/scripts/mls/backend/unified/closed/seed.py --all

    # Seed all except certain MLSs
    python src/scripts/mls/backend/unified/closed/seed.py --all --exclude GPS CRMLS

    # Seed from auto-detected file (most recent)
    python src/scripts/mls/backend/unified/closed/seed.py

    # Seed from specific file
    python src/scripts/mls/backend/unified/closed/seed.py --input local-logs/closed/flattened_closed_GPS_listings.json

    # Recreate indexes only
    python src/scripts/mls/backend/unified/closed/seed.py --indexes-only
"""

import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from pymongo import MongoClient, UpdateOne, GEOSPHERE, ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError, ConnectionFailure, ServerSelectionTimeoutError
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).resolve().parents[6] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("[ERROR] MONGODB_URI is not set in .env.local")

LOCAL_LOGS_DIR = Path(__file__).resolve().parents[6] / "local-logs" / "closed"

# MLS Names (for --exclude validation)
MLS_NAMES = ["GPS", "CRMLS", "CLAW", "SOUTHLAND", "HIGH_DESERT", "BRIDGE", "CONEJO_SIMI_MOORPARK", "ITECH"]


def parse_date(date_str):
    """
    Convert date string to datetime object for MongoDB.
    Handles formats: YYYY-MM-DD, YYYY-MM-DDTHH:MM:SSZ, etc.
    """
    if not date_str or isinstance(date_str, datetime):
        return date_str

    try:
        # Try ISO format first (YYYY-MM-DDTHH:MM:SSZ)
        if 'T' in str(date_str):
            return datetime.fromisoformat(str(date_str).replace('Z', '+00:00'))
        # Try simple date format (YYYY-MM-DD)
        return datetime.strptime(str(date_str), '%Y-%m-%d')
    except Exception as e:
        print(f"[WARN] Could not parse date '{date_str}': {e}")
        return None


def connect_to_mongodb(retries=3):
    """Connect to MongoDB with retry logic"""
    for attempt in range(retries):
        try:
            client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=10000,
                socketTimeoutMS=20000,
            )
            # Force a server selection now
            client.admin.command("ping")
            db = client.get_database()
            print("[OK] Connected to MongoDB")
            return client, db
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            if attempt == retries - 1:
                raise Exception(f"[ERROR] Failed to connect to MongoDB after {retries} attempts: {e}")
            print(f"[WARN] MongoDB connection attempt {attempt + 1} failed, retrying...")
            time.sleep(2 ** attempt)


def create_indexes(collection):
    """
    Create comprehensive indexes for unified_closed_listings collection

    Indexes:
    1. Geospatial (2dsphere) - for CMA radius queries
    2. mlsSource + closeDate - for MLS-specific closed sales
    3. city + closeDate - for city-level appreciation analysis
    4. subdivisionName + closeDate - for subdivision appreciation
    5. propertyType + closeDate - for property type filtering
    5a. propertySubType + closeDate - for single family vs condo/townhouse distinction
    6. listingKey - unique identifier (for upserts)
    7. closePrice + closeDate - for price range queries
    8. address + closeDate - for sales history tracking
    9. TTL index on closeDate - auto-delete after 5 years
    """
    print("\n>>> Creating indexes for unified_closed_listings...")

    # 1. Geospatial index (CRITICAL for CMA radius queries)
    try:
        collection.create_index([("coordinates", GEOSPHERE)], name="coordinates_2dsphere")
        print("[OK] Created geospatial index: coordinates_2dsphere")
    except Exception as e:
        print(f"[WARN] Geospatial index creation failed: {e}")

    # 2. MLS source + closeDate (for MLS-specific queries)
    try:
        collection.create_index(
            [("mlsSource", ASCENDING), ("closeDate", DESCENDING)],
            name="mlsSource_closeDate"
        )
        print("[OK] Created compound index: mlsSource_closeDate")
    except Exception as e:
        print(f"[WARN] MLS index creation failed: {e}")

    # 3. City + closeDate (for city appreciation analysis)
    try:
        collection.create_index(
            [("city", ASCENDING), ("closeDate", DESCENDING)],
            name="city_closeDate"
        )
        print("[OK] Created compound index: city_closeDate")
    except Exception as e:
        print(f"[WARN] City index creation failed: {e}")

    # 4. Subdivision + closeDate (for subdivision appreciation)
    try:
        collection.create_index(
            [("subdivisionName", ASCENDING), ("closeDate", DESCENDING)],
            name="subdivisionName_closeDate",
            sparse=True  # Not all listings have subdivisions
        )
        print("[OK] Created compound index: subdivisionName_closeDate")
    except Exception as e:
        print(f"[WARN] Subdivision index creation failed: {e}")

    # 5. PropertyType + closeDate
    try:
        collection.create_index(
            [("propertyType", ASCENDING), ("closeDate", DESCENDING)],
            name="propertyType_closeDate"
        )
        print("[OK] Created compound index: propertyType_closeDate")
    except Exception as e:
        print(f"[WARN] PropertyType index creation failed: {e}")

    # 5a. PropertySubType + closeDate (CRITICAL for single family vs condo comparisons)
    try:
        collection.create_index(
            [("propertySubType", ASCENDING), ("closeDate", DESCENDING)],
            name="propertySubType_closeDate",
            sparse=True  # Not all property types have subtypes
        )
        print("[OK] Created compound index: propertySubType_closeDate")
    except Exception as e:
        print(f"[WARN] PropertySubType index creation failed: {e}")

    # 6. Unique listingKey index
    try:
        collection.create_index(
            [("listingKey", ASCENDING)],
            name="listingKey_unique",
            unique=True
        )
        print("[OK] Created unique index: listingKey_unique")
    except Exception as e:
        print(f"[WARN] ListingKey index creation failed: {e}")

    # 7. ClosePrice + closeDate (for price range queries)
    try:
        collection.create_index(
            [("closePrice", ASCENDING), ("closeDate", DESCENDING)],
            name="closePrice_closeDate"
        )
        print("[OK] Created compound index: closePrice_closeDate")
    except Exception as e:
        print(f"[WARN] ClosePrice index creation failed: {e}")

    # 8. Address + closeDate (for sales history tracking)
    try:
        collection.create_index(
            [("address", ASCENDING), ("closeDate", DESCENDING)],
            name="address_closeDate",
            sparse=True
        )
        print("[OK] Created compound index: address_closeDate")
    except Exception as e:
        print(f"[WARN] Address index creation failed: {e}")

    # 9. ⭐ TTL INDEX - Auto-delete sales older than 5 years
    try:
        # 157680000 seconds = 5 years (365.25 * 5 * 24 * 60 * 60)
        collection.create_index(
            [("closeDate", ASCENDING)],
            name="closeDate_ttl_5years",
            expireAfterSeconds=157680000
        )
        print("[OK] Created TTL index: closeDate_ttl_5years (auto-delete after 5 years)")
    except Exception as e:
        print(f"[WARN] TTL index creation failed: {e}")

    print("[OK] Index creation complete\n")


def find_latest_flattened_file():
    """Find the most recent flattened closed listings file"""
    if not LOCAL_LOGS_DIR.exists():
        raise FileNotFoundError(f"Directory not found: {LOCAL_LOGS_DIR}")

    # Look for flattened files
    flattened_files = list(LOCAL_LOGS_DIR.glob("flattened_closed_*_listings.json"))

    if not flattened_files:
        raise FileNotFoundError(f"No flattened closed listings files found in {LOCAL_LOGS_DIR}")

    # Return most recently modified
    latest_file = max(flattened_files, key=lambda p: p.stat().st_mtime)
    return latest_file


def seed_listings(collection, input_file, batch_size=500):
    """
    Seed unified_closed_listings collection with bulk upsert

    Args:
        collection: MongoDB collection object
        input_file: Path to flattened JSON file
        batch_size: Number of documents per bulk operation
    """
    print(f"\n>>> Loading closed listings from: {input_file}")

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            listings = json.load(f)
    except Exception as e:
        raise Exception(f"[ERROR] Failed to read {input_file}: {e}")

    if not listings:
        raise Exception("[ERROR] No listings found in file")

    total_listings = len(listings)
    print(f"[OK] Loaded {total_listings:,} closed listings\n")

    # Filter out listings without required fields
    valid_listings = []
    skipped = 0

    for listing in listings:
        # Check for required fields
        if not listing.get("closePrice") or not listing.get("closeDate"):
            skipped += 1
            continue

        # Convert closeDate string to datetime object for MongoDB queries
        listing["closeDate"] = parse_date(listing["closeDate"])

        # Skip if date parsing failed
        if not listing["closeDate"]:
            skipped += 1
            continue

        valid_listings.append(listing)

    if skipped > 0:
        print(f"[WARN] Skipped {skipped} listings missing closePrice or closeDate")

    total_valid = len(valid_listings)
    print(f">>> Seeding {total_valid:,} valid closed listings to MongoDB...\n")

    # Bulk upsert in batches
    total_upserted = 0
    total_modified = 0
    errors = []
    start_time = time.time()

    for i in range(0, total_valid, batch_size):
        batch = valid_listings[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total_valid + batch_size - 1) // batch_size

        # Build bulk operations
        operations = []
        for listing in batch:
            # Use listingKey as unique identifier
            filter_query = {"listingKey": listing["listingKey"]}

            # Upsert operation
            operations.append(
                UpdateOne(
                    filter_query,
                    {"$set": listing},
                    upsert=True
                )
            )

        try:
            result = collection.bulk_write(operations, ordered=False)
            total_upserted += result.upserted_count
            total_modified += result.modified_count

            elapsed = time.time() - start_time
            rate = (i + len(batch)) / elapsed if elapsed > 0 else 0

            print(f"[Batch {batch_num}/{total_batches}] "
                  f"Upserted: {result.upserted_count}, "
                  f"Modified: {result.modified_count} "
                  f"({rate:.0f} docs/sec)")

        except BulkWriteError as e:
            errors.append(f"Batch {batch_num}: {e.details}")
            print(f"[ERROR] Batch {batch_num} failed: {e.details}")

        # Small delay to avoid overwhelming MongoDB
        time.sleep(0.1)

    # Summary
    elapsed_time = time.time() - start_time
    print("\n" + "=" * 80)
    print("SEEDING SUMMARY - CLOSED LISTINGS")
    print("=" * 80)
    print(f"Total processed: {total_valid:,}")
    print(f"Upserted (new): {total_upserted:,}")
    print(f"Modified (existing): {total_modified:,}")
    print(f"Skipped (missing data): {skipped:,}")
    print(f"Errors: {len(errors)}")
    print(f"Time: {elapsed_time:.1f}s ({total_valid/elapsed_time:.0f} docs/sec)")
    print("=" * 80 + "\n")

    if errors:
        print("[WARN] Errors occurred:")
        for error in errors[:5]:  # Show first 5 errors
            print(f"  - {error}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")

    return total_upserted, total_modified


def main():
    parser = argparse.ArgumentParser(description="Seed unified_closed_listings MongoDB collection")
    parser.add_argument(
        "--input",
        type=str,
        help="Path to flattened JSON file. If not provided, uses most recent file in local-logs/closed/"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Seed all flattened MLS files in local-logs/closed/"
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        choices=MLS_NAMES,
        help="MLS associations to exclude when using --all (e.g., --exclude GPS CRMLS)"
    )
    parser.add_argument(
        "--indexes-only",
        action="store_true",
        help="Only recreate indexes, don't seed data"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Documents per bulk operation (default: 500)"
    )

    args = parser.parse_args()

    try:
        print("=" * 80)
        print("Unified MLS Seed - CLOSED LISTINGS")
        print("=" * 80)

        # Connect to MongoDB
        client, db = connect_to_mongodb()
        collection = db["unified_closed_listings"]

        # Create/recreate indexes
        create_indexes(collection)

        if args.indexes_only:
            print("\n[OK] Indexes recreated. Exiting (--indexes-only flag)")
            return

        # Handle --all flag (batch mode)
        if args.all:
            # Find all flattened files
            all_files = sorted(LOCAL_LOGS_DIR.glob("flattened_closed_*_listings.json"))

            if not all_files:
                raise FileNotFoundError(f"No flattened files found in {LOCAL_LOGS_DIR}")

            # Apply exclusions if specified
            if args.exclude:
                excluded = set(args.exclude)
                filtered_files = []
                for file in all_files:
                    # Extract MLS name from filename: flattened_closed_GPS_listings.json -> GPS
                    stem = file.stem  # "flattened_closed_GPS_listings"
                    mls_name = stem.replace("flattened_closed_", "").replace("_listings", "")
                    if mls_name not in excluded:
                        filtered_files.append(file)
                all_files = filtered_files

            if not all_files:
                print("[ERROR] All files were excluded. Nothing to seed.")
                exit(1)

            print(f"\n>>> Batch mode: Seeding {len(all_files)} MLS files")
            if args.exclude:
                print(f">>> Excluded: {', '.join(args.exclude)}")
            print()

            total_upserted = 0
            total_modified = 0

            for idx, input_file in enumerate(all_files, 1):
                # Extract MLS name for display
                stem = input_file.stem
                mls_name = stem.replace("flattened_closed_", "").replace("_listings", "")

                print(f"\n{'#' * 80}")
                print(f"# MLS {idx}/{len(all_files)}: {mls_name}")
                print(f"{'#' * 80}")

                try:
                    upserted, modified = seed_listings(collection, input_file, args.batch_size)
                    total_upserted += upserted
                    total_modified += modified
                except Exception as e:
                    print(f"[ERROR] Failed to seed {mls_name}: {e}")
                    continue

            # Final summary
            total_count = collection.count_documents({})
            print("\n" + "=" * 80)
            print("BATCH SEED SUMMARY")
            print("=" * 80)
            print(f"Files processed: {len(all_files)}")
            print(f"Total upserted: {total_upserted:,}")
            print(f"Total modified: {total_modified:,}")
            print(f"Collection total: {total_count:,} documents")
            print("=" * 80 + "\n")

        else:
            # Single file mode
            # Find input file
            if args.input:
                input_file = Path(args.input)
                if not input_file.exists():
                    raise FileNotFoundError(f"Input file not found: {input_file}")
            else:
                input_file = find_latest_flattened_file()
                print(f"[AUTO] Using latest file: {input_file.name}")

            # Seed data
            upserted, modified = seed_listings(collection, input_file, args.batch_size)

            # Final count
            total_count = collection.count_documents({})
            print(f"\n[OK] Collection 'unified_closed_listings' now has {total_count:,} documents")

        # Close connection
        client.close()
        print("[OK] MongoDB connection closed\n")

    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        exit(1)


if __name__ == "__main__":
    main()
