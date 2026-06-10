import os
import json
import re
from pathlib import Path
from pymongo import MongoClient
from dotenv import load_dotenv
from unidecode import unidecode
from rapidfuzz import fuzz, process

# === Load env from .env.local ===
env_path = Path(__file__).resolve().parents[4] / ".env.local"
load_dotenv(dotenv_path=env_path)

# === MongoDB config ===
MONGO_URI = os.getenv("MONGODB_URI")
DB_NAME = "admin"
COLLECTION_NAME = "listings"

# === Paths ===
ROOT_DIR = Path(__file__).resolve().parents[4]
JSON_DIR = ROOT_DIR / "src" / "app" / "constants" / "subdivisions"
OUTPUT_DIR = ROOT_DIR / "local-logs" / "subdivision-match"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def slugify(text: str) -> str:
    text = unidecode(text).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text).strip("-")

def get_db_subdivision_slugs():
    client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    cursor = collection.find({}, {"subdivisionName": 1})
    slugs = {}

    for doc in cursor:
        name = doc.get("subdivisionName")
        if name and isinstance(name, str):
            slug = slugify(name)
            slugs[slug] = name.strip()

    return slugs

def get_json_subdivision_slugs():
    slugs = {}
    for file in JSON_DIR.glob("*.json"):
        if file.name.startswith("og-"):
            continue  # ❌ Skip old/unused JSONs
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for obj in data:
                    name = obj.get("name")
                    if name:
                        slug = slugify(name)
                        slugs[slug] = {
                            "name": name,
                            "slug": slug,
                            "file": file.name
                        }
        except Exception as e:
            print(f"⚠️ Failed to read {file.name}: {e}")
    return slugs

def compare_and_output():
    db_slugs = get_db_subdivision_slugs()
    json_slugs = get_json_subdivision_slugs()

    matched = {}
    unmatched_db = {}
    unmatched_json = {}

    # === Step 1: Exact slug match
    for slug, db_name in db_slugs.items():
        if slug in json_slugs:
            matched[slug] = {
                "db_name": db_name,
                "json_name": json_slugs[slug]["name"],
                "file": json_slugs[slug]["file"],
                "match_type": "exact"
            }
        else:
            unmatched_db[slug] = db_name

    for slug, json_data in json_slugs.items():
        if slug not in db_slugs:
            unmatched_json[slug] = json_data

    # === Step 2: Fuzzy fallback
    still_unmatched_db = {}

    for db_slug, db_name in unmatched_db.items():
        best_match = process.extractOne(
            db_slug, list(unmatched_json.keys()), scorer=fuzz.token_sort_ratio
        )
        if best_match and best_match[1] >= 80:
            json_slug = best_match[0]
            matched[db_slug] = {
                "db_name": db_name,
                "json_name": unmatched_json[json_slug]["name"],
                "file": unmatched_json[json_slug]["file"],
                "match_type": f"fuzzy:{best_match[1]}"
            }
            del unmatched_json[json_slug]
        else:
            still_unmatched_db[db_slug] = db_name

    unmatched_db = still_unmatched_db

    # === Output Results ===
    with open(OUTPUT_DIR / "matched.json", "w", encoding="utf-8") as f:
        json.dump(matched, f, indent=2)

    with open(OUTPUT_DIR / "unmatched_db.json", "w", encoding="utf-8") as f:
        json.dump(unmatched_db, f, indent=2)

    with open(OUTPUT_DIR / "unmatched_json.json", "w", encoding="utf-8") as f:
        json.dump(unmatched_json, f, indent=2)

    print(f"✅ Matched: {len(matched)} (including fuzzy matches)")
    print(f"❌ In DB but not in JSON: {len(unmatched_db)}")
    print(f"❌ In JSON but not in DB: {len(unmatched_json)}")
    print(f"📄 Results saved in: {OUTPUT_DIR}")

if __name__ == "__main__":
    compare_and_output()
