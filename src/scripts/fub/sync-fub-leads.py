#!/usr/bin/env python3
"""
Follow Up Boss Lead Sync

Incrementally syncs FUB leads (people) into the contacts collection.
Designed for cron execution every 15 minutes.

Usage:
    python src/scripts/fub/sync-fub-leads.py              # Incremental sync
    python src/scripts/fub/sync-fub-leads.py --full        # Full resync (all leads)
    python src/scripts/fub/sync-fub-leads.py --dry-run     # Preview without writing
    python src/scripts/fub/sync-fub-leads.py --verbose     # Detailed logging
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

FUB_API_KEY = os.getenv("FUB_API_KEY")
FUB_BASE_URL = os.getenv("FUB_BASE_URL", "https://api.followupboss.com/v1")
FUB_AGENT_ID = int(os.getenv("FUB_AGENT_ID", "31"))
MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("DATABASE_URL")
JOSEPH_USER_ID = os.getenv("JOSEPH_MONGO_USER_ID")

if not FUB_API_KEY:
    print("ERROR: FUB_API_KEY not set in .env.local")
    sys.exit(1)
if not MONGO_URI:
    print("ERROR: MONGODB_URI not set in .env.local")
    sys.exit(1)
if not JOSEPH_USER_ID:
    print("ERROR: JOSEPH_MONGO_USER_ID not set in .env.local")
    sys.exit(1)

# ---------------------------------------------------------------------------
# FUB Stage -> Contact Status mapping
# ---------------------------------------------------------------------------

STAGE_MAP = {
    # Uncontacted
    "lead": "uncontacted",
    "new": "uncontacted",
    "prospect": "uncontacted",
    "": "uncontacted",
    # Contacted
    "spoke with customer": "contacted",
    "appointment set": "contacted",
    "attempted contact": "contacted",
    # Qualified
    "met with customer": "qualified",
    "showing homes": "qualified",
    "qualified": "qualified",
    # Nurturing
    "nurture": "nurturing",
    "under contract": "nurturing",
    "active": "nurturing",
    "pipeline": "nurturing",
    # Client
    "closed": "client",
    "past client": "client",
    # Inactive
    "inactive": "inactive",
    "unqualified": "inactive",
    "trash": "inactive",
    "do not contact": "inactive",
}


def map_stage(stage_name):
    """Map FUB stage name to Contact status."""
    if not stage_name:
        return "uncontacted"
    return STAGE_MAP.get(stage_name.lower().strip(), "uncontacted")


# ---------------------------------------------------------------------------
# Phone formatting
# ---------------------------------------------------------------------------

def format_e164(phone_str):
    """Format a phone number to E.164 (+1XXXXXXXXXX for US)."""
    if not phone_str:
        return None
    digits = re.sub(r"\D", "", str(phone_str))
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif len(digits) > 11:
        return f"+{digits}"
    return None  # Invalid


def map_phone_type(fub_type):
    """Map FUB phone type to Contact phone label."""
    mapping = {"mobile": "mobile", "home": "home", "work": "work", "fax": "work"}
    return mapping.get((fub_type or "").lower(), "other")


def map_email_type(fub_type):
    """Map FUB email type to Contact email label."""
    mapping = {"home": "personal", "work": "work", "personal": "personal"}
    return mapping.get((fub_type or "").lower(), "other")


# ---------------------------------------------------------------------------
# FUB Person -> Contact mapping
# ---------------------------------------------------------------------------

def map_fub_to_contact(person, user_id):
    """Map a FUB person object to a Contact document."""
    now = datetime.now(timezone.utc)

    # Phones
    phones = []
    legacy_phone = None
    for i, p in enumerate(person.get("phones") or []):
        formatted = format_e164(p.get("value"))
        if not formatted:
            continue
        phones.append({
            "number": formatted,
            "label": map_phone_type(p.get("type")),
            "isPrimary": i == 0 or bool(p.get("isPrimary")),
            "isValid": p.get("status", "").lower() != "invalid",
            "country": "US",
        })
        if i == 0:
            legacy_phone = formatted

    # Emails
    emails = []
    legacy_email = None
    for i, e in enumerate(person.get("emails") or []):
        addr = (e.get("value") or "").strip().lower()
        if not addr:
            continue
        emails.append({
            "address": addr,
            "label": map_email_type(e.get("type")),
            "isPrimary": i == 0 or bool(e.get("isPrimary")),
            "isValid": e.get("status", "").lower() != "invalid",
        })
        if i == 0:
            legacy_email = addr

    # Address
    address = {}
    addresses = person.get("addresses") or []
    if addresses:
        a = addresses[0]
        address = {
            "street": a.get("street", ""),
            "city": a.get("city", ""),
            "state": a.get("state", ""),
            "zip": a.get("code", ""),
            "country": a.get("country", "US"),
        }

    # Status from stage + contacted flag
    status = map_stage(person.get("stage", ""))
    if person.get("contacted") == 1 and status == "uncontacted":
        status = "contacted"

    # Type -> interests
    person_type = (person.get("type") or "").lower()
    tags = list(person.get("tags") or [])
    # Classification tags: "CRM" (umbrella for any managed-CRM integration)
    # and "FUB" (the specific provider). Prepend in that order.
    for sync_tag in ("FUB", "CRM"):
        if sync_tag not in tags:
            tags.insert(0, sync_tag)

    interests = {}
    if person_type == "buyer" or "buyer" in [t.lower() for t in tags]:
        interests["buying"] = True
    if person_type == "seller" or "seller" in [t.lower() for t in tags]:
        interests["selling"] = True
    if person.get("price"):
        interests.setdefault("priceRange", {})["max"] = person["price"]

    doc = {
        "userId": ObjectId(user_id),
        "firstName": person.get("firstName", ""),
        "lastName": person.get("lastName", ""),
        "source": "followupboss",
        "status": status,
        "tags": tags,
        "fubId": person["id"],
        "fubSyncedAt": now,
        "fubData": person,
        "originalData": person,
    }

    if phones:
        doc["phones"] = phones
    if legacy_phone:
        doc["phone"] = legacy_phone
    if emails:
        doc["emails"] = emails
    if legacy_email:
        doc["email"] = legacy_email
    if address:
        doc["address"] = address
    if interests:
        doc["interests"] = interests
    if person.get("lastActivity"):
        doc["lastContactDate"] = person["lastActivity"]

    return doc


# ---------------------------------------------------------------------------
# FUB API fetching
# ---------------------------------------------------------------------------

def fetch_fub_people(since=None, full=False, verbose=False):
    """Fetch all people from FUB API with pagination."""
    people = []
    params = {
        "assignedUserId": FUB_AGENT_ID,
        "sort": "updated",
        "limit": 100,
    }
    if since and not full:
        params["lastActivityAfter"] = since

    url = f"{FUB_BASE_URL}/people"
    page = 0

    while url:
        page += 1
        if verbose:
            print(f"  Fetching page {page}: {url}")

        resp = requests.get(url, auth=(FUB_API_KEY, ""), params=params if page == 1 else None)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            continue

        if resp.status_code != 200:
            print(f"  ERROR: FUB API returned {resp.status_code}: {resp.text[:200]}")
            break

        data = resp.json()
        batch = data.get("people", [])
        people.extend(batch)

        if verbose:
            print(f"  Got {len(batch)} leads (total: {len(people)})")

        # Cursor-based pagination
        next_link = data.get("_metadata", {}).get("nextLink")
        if next_link and batch:
            url = next_link
            time.sleep(0.1)  # Respect rate limits
        else:
            url = None

    return people


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync FUB leads to contacts")
    parser.add_argument("--full", action="store_true", help="Full resync (ignore last sync time)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"FUB Lead Sync — {ts}")
    print(f"{'='*60}")

    # Connect to MongoDB
    client = MongoClient(MONGO_URI)
    db_name = MONGO_URI.rsplit("/", 1)[-1].split("?")[0] if "/" in MONGO_URI else "jpsrealtor"
    db = client[db_name]
    contacts_col = db["contacts"]
    sync_state_col = db["fub_sync_state"]

    # Get last sync time
    since = None
    if not args.full:
        state = sync_state_col.find_one({"_id": "last_sync"})
        if state and state.get("lastSyncedAt"):
            since = state["lastSyncedAt"].strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"Incremental sync since: {since}")
        else:
            print("No previous sync found — doing full sync")

    # Fetch from FUB
    print("Fetching leads from Follow Up Boss...")
    people = fetch_fub_people(since=since, full=args.full, verbose=args.verbose)
    print(f"Fetched {len(people)} leads from FUB")

    if not people:
        print("No leads to sync")
        sync_state_col.update_one(
            {"_id": "last_sync"},
            {"$set": {"lastSyncedAt": datetime.now(timezone.utc), "lastCount": 0}},
            upsert=True,
        )
        return

    # Map and upsert
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    operations = []

    for person in people:
        try:
            doc = map_fub_to_contact(person, JOSEPH_USER_ID)
            fub_id = doc.pop("fubId")

            if args.dry_run:
                name = f"{doc.get('firstName', '')} {doc.get('lastName', '')}".strip()
                phone = doc.get("phone", "N/A")
                print(f"  [DRY RUN] {name} | {phone} | fubId={fub_id} | stage={person.get('stage', 'N/A')}")
                continue

            # Fields we seed from FUB on first insert but never overwrite after:
            # our dashboard / AI owns status + tags going forward, and pushes
            # stage changes back to FUB separately.
            set_on_insert = {
                "importedAt": datetime.now(timezone.utc),
                "createdAt": datetime.now(timezone.utc),
            }
            for owned_field in ("status", "tags"):
                if owned_field in doc:
                    set_on_insert[owned_field] = doc.pop(owned_field)

            operations.append(UpdateOne(
                {"userId": ObjectId(JOSEPH_USER_ID), "fubId": fub_id},
                {
                    "$set": {**doc, "fubId": fub_id},
                    "$setOnInsert": set_on_insert,
                },
                upsert=True,
            ))

        except Exception as e:
            errors += 1
            print(f"  ERROR mapping fubId={person.get('id')}: {e}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would sync {len(people)} leads")
        return

    # Execute bulk write
    if operations:
        result = contacts_col.bulk_write(operations, ordered=False)
        created = result.upserted_count
        updated = result.modified_count
        matched = result.matched_count

        print(f"\nSync complete:")
        print(f"  New contacts:     {created}")
        print(f"  Updated contacts: {updated}")
        print(f"  Matched (no change): {matched - updated}")
        if errors:
            print(f"  Errors:           {errors}")

    # Update sync state
    sync_state_col.update_one(
        {"_id": "last_sync"},
        {"$set": {
            "lastSyncedAt": datetime.now(timezone.utc),
            "lastCount": len(people),
            "lastCreated": created,
            "lastUpdated": updated,
        }},
        upsert=True,
    )

    print(f"\nDone. Total: {len(people)} processed, {created} new, {updated} updated")


if __name__ == "__main__":
    main()
