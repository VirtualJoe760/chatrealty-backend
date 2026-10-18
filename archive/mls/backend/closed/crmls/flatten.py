import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime

# Define paths
project_root = Path(__file__).resolve().parents[6]
input_path = project_root / "local-logs" / "closed" / "crmls" / "all_crmls_closed_listings_with_expansions.json"
output_path = project_root / "local-logs" / "closed" / "crmls" / "flattened_crmls_closed_listings.json"

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
    standard = raw.get("StandardFields", {})
    listing_key = standard.get("ListingKey")
    if not listing_key:
        return None

    output = {}
    output.update(camelize_keys(standard))

    # --- Land details ---
    land = derive_land_details(standard)
    output.update({k: v for k, v in land.items() if v is not None})

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

def run():
    if not input_path.exists():
        raise Exception(f"❌ Input file {input_path} does not exist")

    print(f"📄 Loading CRMLS closed listings from {input_path}")
    with input_path.open(encoding="utf-8") as f:
        listings = json.load(f)

    flattened = []
    skipped = 0

    for item in listings:
        flat = flatten_listing(item)
        if flat:
            flattened.append(flat)
        else:
            skipped += 1

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(flattened, f, indent=2)

    print(f"\n✅ Flattened {len(flattened)} CRMLS closed listings to {output_path}")
    if skipped:
        print(f"⚠️ Skipped {skipped} listings with no ListingKey")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"❌ Error in CRMLS closed flatten.py: {e}")
        exit(1)
