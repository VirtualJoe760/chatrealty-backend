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

MAP_FILE = OUTPUT_DIR / "subdivision_map.json"


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
            continue
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
                            "file": file.name,
                        }
        except Exception as e:
            print(f"⚠️ Failed to read {file.name}: {e}")
    return slugs


def load_existing_map():
    if MAP_FILE.exists():
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_map(mapping):
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    print(f"💾 Saved mapping to {MAP_FILE}")


def interactive_match():
    db_slugs = get_db_subdivision_slugs()
    json_slugs = get_json_subdivision_slugs()
    existing_map = load_existing_map()

    updated_map = dict(existing_map)

    for db_slug, db_name in db_slugs.items():
        # Already mapped? Skip
        if db_slug in existing_map:
            continue

        # Try exact
        if db_slug in json_slugs:
            updated_map[db_slug] = {
                "db_name": db_name,
                "json_slug": db_slug,
                "json_name": json_slugs[db_slug]["name"],
                "json_file": json_slugs[db_slug]["file"],
                "match_type": "exact",
            }
            continue

        # Try fuzzy
        best_match = process.extractOne(
            db_slug, list(json_slugs.keys()), scorer=fuzz.token_sort_ratio
        )

        if best_match:
            json_slug, score = best_match[0], best_match[1]
            json_data = json_slugs[json_slug]

            if score >= 85:
                # Confirm with user
                print("\n--------------------------------------")
                print(f"❓ DB subdivision: {db_name} (slug: {db_slug})")
                print(
                    f"👉 Suggested JSON: {json_data['name']} "
                    f"(slug: {json_slug}, file: {json_data['file']}, score: {score})"
                )
                choice = input("Does this match? [y/n]: ").strip().lower()
                if choice == "y":
                    updated_map[db_slug] = {
                        "db_name": db_name,
                        "json_slug": json_slug,
                        "json_name": json_data["name"],
                        "json_file": json_data["file"],
                        "match_type": f"user-confirmed:{score}",
                    }
                    continue

        # Manual selection fallback
        print("\n--------------------------------------")
        print(f"❓ DB subdivision: {db_name} (slug: {db_slug})")

        # Find top 5 fuzzy matches only (limit + threshold)
        matches = process.extract(
            db_slug,
            list(json_slugs.keys()),
            scorer=fuzz.token_sort_ratio,
            limit=5,
        )
        strong_matches = [(slug, score) for slug, score, _ in matches if score >= 70]

        if not strong_matches:
            print("⚠️ No strong matches found. Skipping.")
            continue

        print("Please choose the best match:")
        for idx, (slug, score) in enumerate(strong_matches, start=1):
            print(f"[{idx}] {json_slugs[slug]['name']} ({json_slugs[slug]['file']}, score: {score})")
        print("[0] Skip for now")

        while True:
            try:
                selection = int(input("> "))
                if selection == 0:
                    break
                elif 1 <= selection <= len(strong_matches):
                    chosen_slug, chosen_score = strong_matches[selection - 1]
                    chosen_data = json_slugs[chosen_slug]
                    updated_map[db_slug] = {
                        "db_name": db_name,
                        "json_slug": chosen_slug,
                        "json_name": chosen_data["name"],
                        "json_file": chosen_data["file"],
                        "match_type": f"manual:{chosen_score}",
                    }
                    break
                else:
                    print("Invalid choice, try again.")
            except ValueError:
                print("Please enter a number.")

    save_map(updated_map)


if __name__ == "__main__":
    interactive_match()
