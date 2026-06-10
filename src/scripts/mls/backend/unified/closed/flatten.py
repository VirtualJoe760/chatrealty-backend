#!/usr/bin/env python3
"""
Unified MLS Flatten Script - CLOSED LISTINGS

Flattens closed listings from fetch.py output to camelCase format with:
- MLS source tracking (mlsSource, mlsId)
- PropertyType name mapping (propertyTypeName)
- Closed-specific fields (closePrice, closeDate)
- Address slugification
- Null/empty/masked field removal

Usage:
    # Flatten most recent closed listings file
    python src/scripts/mls/backend/unified/closed/flatten.py

    # Flatten specific file
    python src/scripts/mls/backend/unified/closed/flatten.py --input local-logs/closed/closed_5y_GPS_listings.json

    # Process all closed listings files
    python src/scripts/mls/backend/unified/closed/flatten.py --all

Input: local-logs/closed/closed_5y_{MLS}_listings.json (from fetch.py)
Output: local-logs/closed/flattened_closed_{MLS}_listings.json
"""

import json
import re
import unicodedata
import argparse
from pathlib import Path
from datetime import datetime

# MLS ID to Name Mapping (reverse lookup)
MLS_ID_TO_NAME = {
    "20190211172710340762000000": "GPS",
    "20200218121507636729000000": "CRMLS",
    "20200630203341057545000000": "CLAW",
    "20200630203518576361000000": "SOUTHLAND",
    "20200630204544040064000000": "HIGH_DESERT",
    "20200630204733042221000000": "BRIDGE",
    "20160622112753445171000000": "CONEJO_SIMI_MOORPARK",
    "20200630203206752718000000": "ITECH"
}

# PropertyType Name Mapping
PROPERTY_TYPE_NAMES = {
    'A': 'Residential',
    'B': 'Residential Lease',
    'C': 'Residential Income',
    'D': 'Land',
    'E': 'Commercial Sale',
    'F': 'Commercial Lease',
    'G': 'Business Opportunity',
    'H': 'Manufactured In Park',
    'I': 'Mobile Home'
}


def to_camel_case(s: str) -> str:
    """Convert PascalCase to camelCase"""
    parts = re.sub(r'(?<!^)(?=[A-Z])', '_', s).lower().split('_')
    return parts[0] + ''.join(word.capitalize() for word in parts[1:])


def simple_slugify(value: str) -> str:
    """Create URL-safe slug from address"""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[\s]+", "-", value)


def extract_bool_keys(d: dict) -> str | None:
    """Extract keys where value is True, join with commas"""
    if not isinstance(d, dict):
        return None
    keys = [k for k, v in d.items() if v is True]
    return ", ".join(keys) if keys else None


def camelize_keys(obj):
    """Recursively convert all keys to camelCase and remove nulls/empties"""
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if v in (None, "********", [], {}):
                continue
            camel_key = to_camel_case(k)
            # Flatten boolean dicts (e.g., {Elevator: true, Pool: true} -> "Elevator, Pool")
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


def derive_land_details(standard: dict) -> dict:
    """
    Derives landType, lease amount, frequency, and years remaining if available.
    Handles malformed expiration date formats gracefully.
    """
    land_lease_type = standard.get("LandLeaseType")
    land_lease_yn = standard.get("LandLeaseYN")
    ownership_type = standard.get("OwnershipType")
    land_lease_amount = standard.get("LandLeaseAmount")
    land_lease_per = standard.get("LandLeasePer")
    expiration_fields = [
        standard.get("LandLeaseExpirationDate"),
        standard.get("LeaseExpirationDate"),
        standard.get("LandLeaseExpirationYear")
    ]

    land_type = None
    if isinstance(land_lease_type, str):
        val = land_lease_type.strip().lower()
        if "fee" in val:
            land_type = "Fee"
        elif "lease" in val:
            land_type = "Lease"

    if land_type is None and isinstance(land_lease_yn, bool):
        land_type = "Lease" if land_lease_yn else "Fee"

    if land_type is None and isinstance(ownership_type, str):
        val = ownership_type.strip().lower()
        if "fee" in val:
            land_type = "Fee"
        elif "lease" in val:
            land_type = "Lease"

    if land_type is None:
        land_type = "Fee"

    # Derive years remaining if expiration is known
    current_year = datetime.now().year
    lease_expiration_date = None
    lease_years_remaining = None

    for field in expiration_fields:
        if not isinstance(field, str):
            continue

        # Match proper YYYY-MM-DD formats
        if re.match(r"\d{4}-\d{2}-\d{2}", field):
            lease_expiration_date = field
            try:
                exp_year = int(field.split("-")[0])
                lease_years_remaining = exp_year - current_year
            except ValueError:
                pass
            break

        # Match messy or partial year strings (e.g. "11302069-01-01" or "2067")
        match = re.search(r"(\d{4})", field)
        if match:
            try:
                exp_year = int(match.group(1))
                lease_expiration_date = f"{exp_year}-12-31"
                lease_years_remaining = exp_year - current_year
            except ValueError:
                pass
            break

    return {
        "landType": land_type,
        "landLeaseAmount": land_lease_amount if isinstance(land_lease_amount, (int, float)) else None,
        "landLeasePer": land_lease_per if isinstance(land_lease_per, str) else None,
        "landLeaseExpirationDate": lease_expiration_date,
        "landLeaseYearsRemaining": lease_years_remaining if lease_years_remaining and lease_years_remaining > 0 else None,
    }


def flatten_listing(raw: dict) -> dict | None:
    """
    Flatten a single closed listing from RESO format to camelCase with enhanced fields.

    Enhancements for unified MLS closed listings:
    - mlsSource: Human-readable MLS name (GPS, CRMLS, etc.)
    - mlsId: 26-digit MLS ID
    - propertyTypeName: Human-readable property type (Residential, Land, etc.)
    - closePrice: Sale price
    - closeDate: Date sold
    - coordinates: GeoJSON point for geospatial queries
    """
    standard = raw.get("StandardFields", {})
    listing_key = standard.get("ListingKey")
    if not listing_key:
        return None

    # CRITICAL: Skip listings without closePrice or closeDate
    if not standard.get("ClosePrice") or not standard.get("CloseDate"):
        return None

    output = {}
    output.update(camelize_keys(standard))

    # --- Enhanced MLS Source Tracking ---
    mls_id = standard.get("MlsId")
    if mls_id:
        output["mlsId"] = mls_id
        output["mlsSource"] = MLS_ID_TO_NAME.get(mls_id, "UNKNOWN")

    # --- Enhanced PropertyType Name ---
    property_type = standard.get("PropertyType")
    if property_type:
        output["propertyTypeName"] = PROPERTY_TYPE_NAMES.get(property_type, property_type)

    # --- Land details ---
    land = derive_land_details(standard)
    output.update({k: v for k, v in land.items() if v is not None})

    # --- GeoJSON coordinates for geospatial indexing (2dsphere) ---
    latitude = standard.get("Latitude")
    longitude = standard.get("Longitude")
    if latitude is not None and longitude is not None:
        output["coordinates"] = {
            "type": "Point",
            "coordinates": [float(longitude), float(latitude)]  # [lng, lat] for GeoJSON
        }

    # --- Merge top-level fields ---
    for key, value in raw.items():
        if key == "StandardFields" or value in (None, "********", [], {}):
            continue
        camel_key = to_camel_case(key)
        if camel_key not in output:
            output[camel_key] = camelize_keys(value)

    # --- Slugify address ---
    unparsed = standard.get("UnparsedAddress") or raw.get("UnparsedAddress")
    slug_address = simple_slugify(unparsed) if unparsed else "unknown"

    final = {
        "slug": listing_key,
        "slugAddress": slug_address,
    }
    final.update(output)
    return final


def run(input_file: Path, output_file: Path):
    """Process closed listings from input file and write flattened output"""
    if not input_file.exists():
        raise Exception(f"Input file {input_file} does not exist")

    print(f"\n>>> Loading closed listings from {input_file.name}")
    with input_file.open(encoding="utf-8") as f:
        listings = json.load(f)

    print(f">>> Processing {len(listings):,} closed listings...")

    flattened = []
    skipped = 0

    for item in listings:
        flat = flatten_listing(item)
        if flat:
            flattened.append(flat)
        else:
            skipped += 1

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(flattened, f, indent=2)

    print(f">>> Flattened {len(flattened):,} closed listings to {output_file.name}")
    if skipped:
        print(f">>> Skipped {skipped} listings with missing ListingKey/ClosePrice/CloseDate\n")

    return flattened


def main():
    parser = argparse.ArgumentParser(description="Flatten unified MLS closed listings")
    parser.add_argument(
        "--input",
        type=str,
        help="Input JSON file path (default: auto-detect from local-logs/closed)"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON file path (default: flattened_closed_*.json)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all closed_5y_*_listings.json files in local-logs/closed"
    )

    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[6]
    local_logs = project_root / "local-logs" / "closed"

    if not local_logs.exists():
        raise Exception(f"Directory not found: {local_logs}")

    # Process all files if --all flag is set
    if args.all:
        candidates = sorted(local_logs.glob("closed_5y_*_listings.json"))
        if not candidates:
            raise Exception(f"No closed listings files found in {local_logs}")

        print("=" * 80)
        print("Unified MLS Flatten - CLOSED LISTINGS (Batch Mode)")
        print("=" * 80)
        print(f"Found {len(candidates)} files to process\n")

        total_flattened = 0
        for input_path in candidates:
            # Extract MLS name from filename: closed_5y_GPS_listings.json -> GPS
            stem = input_path.stem  # "closed_5y_GPS_listings"
            mls_name = stem.replace("closed_5y_", "").replace("_listings", "")
            output_name = f"flattened_closed_{mls_name}_listings.json"
            output_path = local_logs / output_name

            try:
                print(f"{'#' * 80}")
                print(f"# Processing: {mls_name}")
                print(f"{'#' * 80}")
                flattened = run(input_path, output_path)
                total_flattened += len(flattened)
                print()
            except Exception as e:
                print(f"[ERROR] Failed to process {input_path.name}: {e}\n")
                continue

        print("\n" + "=" * 80)
        print("BATCH FLATTEN SUMMARY")
        print("=" * 80)
        print(f"Files processed: {len(candidates)}")
        print(f"Total flattened: {total_flattened:,}")
        print("=" * 80 + "\n")
        return

    # Single file mode
    # Auto-detect input file if not specified
    if args.input:
        input_path = Path(args.input)
    else:
        # Look for most recent closed_5y_*_listings.json
        candidates = sorted(local_logs.glob("closed_5y_*_listings.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise Exception(f"No closed listings files found in {local_logs}. Run fetch.py first.")
        input_path = candidates[0]

    # Auto-generate output file if not specified
    if args.output:
        output_path = Path(args.output)
    else:
        # Convert closed_5y_GPS_listings.json -> flattened_closed_GPS_listings.json
        stem = input_path.stem  # "closed_5y_GPS_listings"
        mls_part = stem.replace("closed_5y_", "")  # "GPS_listings"
        output_name = f"flattened_closed_{mls_part}.json"
        output_path = local_logs / output_name

    try:
        print("=" * 80)
        print("Unified MLS Flatten - CLOSED LISTINGS")
        print("=" * 80)

        flattened = run(input_path, output_path)

        # Summary
        print("\n" + "=" * 80)
        print("Summary:")
        print(f"  Input: {input_path}")
        print(f"  Output: {output_path}")
        print(f"  Total flattened: {len(flattened):,}")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        exit(1)


if __name__ == "__main__":
    main()
