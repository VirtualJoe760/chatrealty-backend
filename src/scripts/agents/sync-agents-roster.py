#!/usr/bin/env python3
"""
Sync the Spark member rosters into `agent_roster`.

Why this exists: `agents` is built from listing data, which carries production
and location but almost no contact detail. Measured on the 240,236-doc agents
collection 2026-10-14, before this script existed:

    licence number   2,517  (1.0%)
    email            2,520  (1.0%)

because the only roster source was `mlsagents` — 4,720 docs, the desert MLS
only. Spark's /v1/accounts endpoint serves the member roster for **all eight**
MLSs this platform syncs, 158,667 members in total, and the per-MLS field
coverage is good (sampled 400 members per MLS, 2026-10-14):

    MLS                   members   ShortId  Licence   Email   Phone   Photo
    GPS                     4,366    100.0%    92.0%   97.8%   68.8%   41.8%
    CRMLS                 114,812     99.8%    94.2%   95.2%   84.8%    1.0%
    CLAW                   16,513    100.0%    95.0%   99.8%   99.0%    0.0%
    SOUTHLAND               9,432    100.0%    99.0%  100.0%   97.8%    0.0%
    BRIDGE                  4,114    100.0%    93.0%  100.0%   94.8%    0.0%
    ITECH                   4,156    100.0%    90.5%   97.8%   80.8%    0.0%
    HIGH_DESERT             3,468    100.0%    89.5%  100.0%   48.2%    0.0%
    CONEJO_SIMI_MOORPARK    1,806    100.0%    96.0%   99.8%   78.0%   48.8%

Photos are the exception: only GPS and Conejo syndicate member images at all,
so an agent photo is a desert-market feature, not a platform-wide one.

── The join key ─────────────────────────────────────────────────────────────
`ShortId` (and its twin `OriginatingSystemMlsId`) is the member's per-MLS short
code — "d86130", "MRM-CCBE-206531882" — which is exactly the value listings
carry in `listAgentMlsId`. So the roster joins to `agents` on

    agentRef = "<mlsSource>:<lower(ShortId)>"   ==   agents._id

and NOT on the 26-digit Spark `Id`. That matters: `Id` corresponds to
`agents.sparkKey`, which is only 22-35% populated, and a join on it returns
nothing at all for CRMLS, HIGH_DESERT, BRIDGE or ITECH. Measured on a
3,199-member sample, ShortId matched an agent record for all eight MLSs while
sparkKey matched for only four; together 56.7% of sampled members matched.

The ~43% that do not match are licensed members with no listing or closing
inside the 5-year window — they are simply not in `agents`, which is correct.
This collection keeps them anyway, so a lookup by name or licence can still
find an agent who has not transacted recently.

── Ownership ────────────────────────────────────────────────────────────────
This writes a NEW collection and leaves `mlsagents` alone. `mlsagents` is
written by something outside this repo (no producer here) and may still have
readers, so it is treated as read-only; `build-agents.py` prefers
`agent_roster` and falls back to it.

Usage:
    python3 src/scripts/agents/sync-agents-roster.py --all
    python3 src/scripts/agents/sync-agents-roster.py --mls GPS --verbose
    python3 src/scripts/agents/sync-agents-roster.py --all --dry-run
    python3 src/scripts/agents/sync-agents-roster.py --indexes-only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import OperationFailure

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI")
ACCESS_TOKEN = os.getenv("SPARK_ACCESS_TOKEN")
if not MONGO_URI:
    raise SystemExit("Missing MONGODB_URI in .env.local")
if not ACCESS_TOKEN:
    raise SystemExit("Missing SPARK_ACCESS_TOKEN in .env.local")

# replication.sparkapi.com only — the plain sparkapi.com host answers 403
# "This key is restricted from accessing this endpoint" for this key.
BASE_URL = "https://replication.sparkapi.com/v1/accounts"
ROSTER = "agent_roster"

# Same ids as src/scripts/mls/backend/unified/unified-fetch.py. Kept in step
# with that file by hand; if an MLS is added there, add it here too.
MLS_IDS = {
    "GPS": "20190211172710340762000000",
    "CRMLS": "20200218121507636729000000",
    "CLAW": "20200630203341057545000000",
    "SOUTHLAND": "20200630203518576361000000",
    "HIGH_DESERT": "20200630204544040064000000",
    "BRIDGE": "20200630204733042221000000",
    "CONEJO_SIMI_MOORPARK": "20160622112753445171000000",
    "ITECH": "20200630203206752718000000",
}

PAGE_SIZE = 500
BULK = 500
RETRIES = 3
RETRY_SLEEP = 3
PAGE_SLEEP = 0.2

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "X-SparkApi-User-Agent": "chatrealty-backend",
    "Accept": "application/json",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _pick(rows, key):
    """Primary entry's value, else the first non-empty one.

    Spark returns Emails/Phones/Images/Websites as lists of
    {Id, Name, Type, Primary, <value key>}; `Primary` is a bool and is not
    always present, hence the fallback rather than a filter.
    """
    if not rows:
        return None, []
    vals = [str(r.get(key)).strip() for r in rows
            if isinstance(r, dict) and r.get(key) not in (None, "")]
    vals = [v for v in vals if v]
    if not vals:
        return None, []
    primary = next((str(r.get(key)).strip() for r in rows
                    if isinstance(r, dict) and r.get("Primary")
                    and r.get(key) not in (None, "")), None)
    return (primary or vals[0]), sorted(dict.fromkeys(vals))


def _ts(v):
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_doc(acct: dict, mls_source: str, now, build_id: str) -> dict | None:
    spark_id = acct.get("Id")
    if not spark_id:
        return None

    short_id = (acct.get("ShortId") or acct.get("OriginatingSystemMlsId") or "")
    short_id = str(short_id).strip()

    email, emails = _pick(acct.get("Emails"), "Address")
    phone, phones = _pick(acct.get("Phones"), "Number")
    photo, photos = _pick(acct.get("Images"), "Uri")
    site, sites = _pick(acct.get("Websites"), "Uri")

    addr = next((a for a in (acct.get("Addresses") or [])
                 if isinstance(a, dict) and a.get("Primary")), None)
    if addr is None:
        addr = next((a for a in (acct.get("Addresses") or [])
                     if isinstance(a, dict)), None)

    doc = {
        "_id": str(spark_id),
        "sparkId": str(spark_id),
        "mlsSource": mls_source,
        "mlsSystemId": acct.get("MlsId"),
        "mlsName": acct.get("Mls"),
        # The join key into agents._id. Absent when the feed gives no ShortId
        # (0.2% of CRMLS); such a member can still be found by licence or name.
        "agentRef": f"{mls_source}:{short_id.lower()}" if short_id else None,
        "shortId": short_id or None,
        "originatingSystemMlsId": acct.get("OriginatingSystemMlsId"),
        "name": acct.get("Name"),
        "firstName": acct.get("FirstName"),
        "middleName": acct.get("MiddleName"),
        "lastName": acct.get("LastName"),
        "marketingName": acct.get("MarketingName"),
        "suffix": acct.get("Suffix"),
        "licenseNumber": acct.get("LicenseNumber"),
        "email": email,
        "emails": emails,
        "phone": phone,
        "phones": phones,
        "photoUrl": photo,
        "photoUrls": photos,
        "website": site or None,
        "websites": sites,
        "bio": acct.get("Biography"),
        "office": acct.get("Office"),
        "officeId": acct.get("OfficeId"),
        "officeShortId": acct.get("OfficeShortId"),
        "company": acct.get("Company"),
        "companyId": acct.get("CompanyId"),
        "officeCity": (addr or {}).get("City"),
        "officePostalCode": (addr or {}).get("PostalCode"),
        "officeAddress": (addr or {}).get("Address"),
        "associations": [a for a in (acct.get("Associations") or []) if a],
        "designations": list((acct.get("Designations") or {}).keys()),
        "languagesSpoken": [l for l in (acct.get("LanguagesSpoken") or []) if l],
        "localType": acct.get("LocalType"),
        "userType": acct.get("UserType"),
        "userLevel": acct.get("UserLevel"),
        "active": acct.get("Active"),
        "systemAccess": acct.get("SystemAccess"),
        "idxParticipant": acct.get("IdxParticipant"),
        "countyOrParish": acct.get("CountyOrParish"),
        "modificationTimestamp": _ts(acct.get("ModificationTimestamp")),
        "originalEntryTimestamp": _ts(acct.get("OriginalEntryTimestamp")),
        "syncedAt": now,
        "buildId": build_id,
    }
    return doc


def fetch_mls(mls_source: str, mls_id: str, *, page_cap=None, verbose=False):
    """Yield pages of member accounts for one MLS via _skiptoken pagination."""
    flt = f"MlsId Eq '{mls_id}' And UserType Eq 'Member'"

    total = None
    try:
        r = requests.get(BASE_URL, headers=HEADERS, timeout=60,
                         params={"_filter": flt, "_pagination": "count"})
        if r.status_code == 200:
            total = ((r.json().get("D") or {}).get("Pagination") or {}).get("TotalRows")
    except requests.RequestException:
        pass
    print(f"  {mls_source:22s} roster size: "
          f"{total if total is not None else 'unknown'}", flush=True)

    skiptoken, page, seen = "", 0, 0
    while True:
        params = {"_filter": flt, "_limit": PAGE_SIZE, "_skiptoken": skiptoken}
        batch, new_token, ok = [], None, False

        for attempt in range(RETRIES):
            try:
                r = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=90)
                if r.status_code == 200:
                    d = r.json().get("D") or {}
                    batch = d.get("Results") or []
                    new_token = d.get("SkipToken")
                    ok = True
                    break
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(RETRY_SLEEP * (attempt + 1))
                    continue
                print(f"    ! HTTP {r.status_code}: {r.text[:160]}", flush=True)
                return
            except requests.RequestException as e:
                if attempt == RETRIES - 1:
                    print(f"    ! {type(e).__name__}: {str(e)[:120]}", flush=True)
                    return
                time.sleep(RETRY_SLEEP * (attempt + 1))

        if not ok or not batch:
            return

        page += 1
        seen += len(batch)
        yield batch
        if verbose:
            print(f"    page {page:>4}  +{len(batch):<4} = {seen:>7,}"
                  f"{f' / {total:,}' if total else ''}", flush=True)

        # Same end conditions as unified-fetch.py: no token, or an unchanged one.
        if not new_token or new_token == skiptoken:
            return
        if page_cap and page >= page_cap:
            print(f"    (page cap {page_cap} reached)", flush=True)
            return
        skiptoken = new_token
        time.sleep(PAGE_SLEEP)


INDEXES = [
    ([("agentRef", ASCENDING)], {"name": "agentRef_1", "sparse": True}),
    ([("shortId", ASCENDING)], {"name": "shortId_1", "sparse": True}),
    ([("mlsSource", ASCENDING), ("shortId", ASCENDING)], {"name": "mlsSource_shortId"}),
    ([("licenseNumber", ASCENDING)], {"name": "licenseNumber_1", "sparse": True}),
    ([("email", ASCENDING)], {"name": "email_1", "sparse": True}),
    ([("officeId", ASCENDING)], {"name": "officeId_1", "sparse": True}),
    ([("name", ASCENDING)], {"name": "name_1"}),
]


def ensure_indexes(db):
    for keys, opts in INDEXES:
        try:
            db[ROSTER].create_index(keys, background=True, **opts)
        except OperationFailure as e:
            print(f"  ! index {opts.get('name')}: {str(e)[:110]}", flush=True)
    print(f"  ensured {len(INDEXES)} indexes on {ROSTER}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Sync Spark member rosters")
    ap.add_argument("--all", action="store_true", help="every MLS (default)")
    ap.add_argument("--mls", choices=list(MLS_IDS), help="one MLS only")
    ap.add_argument("--page-cap", type=int, help="stop after N pages per MLS")
    ap.add_argument("--indexes-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="fetch but do not write")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    db = MongoClient(MONGO_URI).get_database()
    now = datetime.now(timezone.utc)
    build_id = now.strftime("%Y%m%dT%H%M%SZ")

    if args.indexes_only:
        ensure_indexes(db)
        return

    targets = {args.mls: MLS_IDS[args.mls]} if args.mls else dict(MLS_IDS)

    print(f"\n{'='*72}")
    print(f"  sync-agents-roster  db={db.name}  build={build_id}")
    print(f"  {len(targets)} MLS(s): {', '.join(targets)}"
          f"{'   [DRY RUN]' if args.dry_run else ''}")
    print(f"{'='*72}\n")

    t0 = time.time()
    grand, skipped = 0, 0

    for mls_source, mls_id in targets.items():
        got, ops = 0, []
        for batch in fetch_mls(mls_source, mls_id,
                               page_cap=args.page_cap, verbose=args.verbose):
            for acct in batch:
                doc = to_doc(acct, mls_source, now, build_id)
                if doc is None:
                    skipped += 1
                    continue
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
                got += 1
            if len(ops) >= BULK and not args.dry_run:
                db[ROSTER].bulk_write(ops, ordered=False)
                ops = []
        if ops and not args.dry_run:
            db[ROSTER].bulk_write(ops, ordered=False)
        grand += got
        print(f"  {mls_source:22s} -> {got:>7,} members", flush=True)

    print(f"\n  total members: {grand:,}"
          f"{f'  ({skipped} skipped for no Id)' if skipped else ''}")

    if not args.dry_run:
        ensure_indexes(db)
        have = db[ROSTER].count_documents({})
        joinable = db[ROSTER].count_documents({"agentRef": {"$ne": None}})
        with_email = db[ROSTER].count_documents({"email": {"$nin": [None, ""]}})
        with_lic = db[ROSTER].count_documents({"licenseNumber": {"$nin": [None, ""]}})
        print(f"\n  {ROSTER}: {have:,} docs"
              f"  | joinable {joinable:,}"
              f"  | email {with_email:,}"
              f"  | licence {with_lic:,}")

    print(f"\ndone in {(time.time()-t0)/60:.1f} min\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
