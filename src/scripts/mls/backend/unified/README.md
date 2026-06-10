# Unified MLS Data Pipeline

**Last Updated**: 2026-04-22
**Status**: Production (running nightly via cron)
**Coverage**: 8 MLS associations · 4 lifecycle collections

The unified pipeline is the authoritative path for MLS listings on jpsrealtor.com. It fetches from Spark Replication API, normalizes to camelCase, and routes each listing to the correct lifecycle collection based on its current status.

---

## Lifecycle model

Every listing from Spark flows through one of four MongoDB collections depending on its `standardStatus`:

| Collection | Status values | Purpose |
|---|---|---|
| `unified_listings` | `Active`, `ComingSoon` | Publicly visible (maps, search, listing pages) |
| `unified_in_escrow` | `Pending`, `Active Under Contract` | Transaction in progress. Photos preserved. Not used for CMA. |
| `unified_off_market` | `Hold`, `Expired`, `Canceled`, `Withdrawn`, `OffMarket` | Didn't sell. Kept separate so CMA stats aren't polluted by non-sold off-market listings. |
| `unified_closed_listings` | `Closed` | Sold properties. The CMA comp source. |

The status router in `seed.py` places each fresh doc in the correct collection and deletes any stale copy from the other three. A listing that flips `Active → Pending` moves cleanly; no duplicates.

---

## Directory layout

```
unified/
├── README.md                    ← you are here
├── unified-fetch.py             ← Spark Replication API fetch (on-market statuses)
├── flatten.py                   ← Raw → camelCase flatten + Media preservation
├── seed.py                      ← Status-router: upserts into correct lifecycle collection
├── run-pipeline.py              ← Orchestrator: fetch → flatten → seed
├── update-status.py             ← Batched full-data refresher + cross-collection router
├── fetch-photos.py              ← Per-listing /photos → media[] inline (all 3 on-market collections)
├── cache-photos.py              ← Per-listing primary photo → db.photos (list/map views)
├── verify-db.py                 ← Read-only sanity check (counts, statuses, indexes)
├── main.py                      ← Legacy orchestrator (kept for compatibility)
└── closed/
    ├── README.md
    ├── fetch.py                 ← Closed-sales fetch (5-year rolling)
    ├── flatten.py
    └── seed.py                  ← Seeds directly into unified_closed_listings
```

---

## Nightly cron schedule

All scripts run under `/root/jpsrealtor/.venv/bin/python3`. Source of truth: `/root/crontab.vps`.

| Time | Command | What it does |
|---|---|---|
| **06:00 Mon–Sat** | `run-pipeline.py --all --incremental` | Fetch (25h window), flatten, router-seed for all 8 MLSs. ~3–5 min. |
| **03:00 Sun** | `run-pipeline.py --all` | Weekly full sync (no time filter) — catches drift. |
| **07:00 daily** | `update-status.py` | Batched (25/req) full-data refresh of every on-market doc. Routes status transitions to correct collection. Sweeps stuck Closed docs. ~90 min for ~125k docs. |
| **08:00 daily** | `cma/build-subdivision-cma.py --all` | Rebuild `cmaStats` on every subdivision. ~55 min. |
| **09:00 daily** | `cache-photos.py` | Cache primary photos into `db.photos` (list-view fast path). Long-running. |
| **09:30 daily** | `fetch-photos.py --all --delta` | Media[] inline delta sync on all 3 on-market collections. ~6 hours for full catch-up. |
| **10:00 daily** | `backend/historical_closed_sync.py` | 30-day rolling closings into `unified_closed_listings`. |

Two entries are commented-out in `crontab.vps` until their scripts are rebuilt:
- `*/5 * * * * check-article-requests-simple.js`
- `0 2 * * * backup_payload_db.sh`

---

## Scripts — what they do

### `unified-fetch.py`
Pulls listings from Spark for one or more MLSs.

- **Default status filter** covers the full on-market lifecycle so the router places them correctly: `Active, ComingSoon, Pending, Active Under Contract, Hold`.
- **Incremental window** defaults to **25 hours** (`--hours N` to override). The 1-hour overlap over the 24h cron interval guarantees no gap.
- **Media fallback**: BRIDGE and ITECH reject `_expand=Media` with HTTP 400. On that error, the script auto-retries without Media and logs the MLS. Other expansions (OpenHouses, VirtualTours) remain.
- Output: `local-logs/{all|incremental}_<MLS>_listings.json`.

```bash
# Daily incremental (cron does this)
python3 unified-fetch.py --mls GPS --incremental

# One-off 7-day window
python3 unified-fetch.py --mls CRMLS --incremental --hours 168
```

### `flatten.py`
Converts Spark PascalCase payloads to camelCase. Preserves `Media[]` from the expansion for photo caching. Auto-detects the newest file in `local-logs/` (either `all_*` or `incremental_*`).

### `seed.py` — status router
Core behavioral change from the pre-2026-04 era: **seed is no longer "insert into unified_listings."** It reads the newest flattened file and routes each listing to one of the four lifecycle collections based on `standardStatus`. When a listing's new target differs from where it already lives, the router bulk-deletes from the other collections to prevent duplicates.

```bash
python3 seed.py                     # auto-detect newest flattened file; route
python3 seed.py --indexes-only      # ensure indexes on all 4 lifecycle collections, exit
```

**Indexes created on every lifecycle collection:** `listingKey_unique`, `mlsSource_mlsId`, `standardStatus_1`, `modificationTimestamp_desc`, `coordinates_2dsphere`, `city_status`, `subdivision_status`, `propertyType_status`, `statusLastChecked_1`, `lastSparkMissAt_1`.

### `run-pipeline.py`
Thin orchestrator that chains fetch → flatten → seed per MLS. Used by the 6am and Sunday-3am cron entries.

### `update-status.py` — batched refresher + router
Runs daily at 7am. Replaces the earlier per-listing minimal-payload status check. Current behavior:

1. **Sweep** — any doc already marked `Closed` in an on-market collection moves to `unified_closed_listings`.
2. **Iterate** listingKeys across `unified_listings`, `unified_in_escrow`, `unified_off_market`.
3. **Batch** 25 keys per Spark request with full `StandardFields` + Media/OpenHouses/VirtualTours expansions.
4. **Flatten** each returned doc.
5. **Route** by fresh status to the correct collection. Upsert there; if the source was different, delete from source.
6. **Miss handling**: Spark doesn't return OffMarket/withdrawn listings. On first miss, stamp `lastSparkMissAt` and `sparkMissCount`. On the 2nd consecutive miss, move to `unified_off_market`. (The two-miss threshold avoids a repeat of the Apr 6 purge-stale incident.)

```bash
python3 update-status.py                    # daily run
python3 update-status.py --source unified_in_escrow
python3 update-status.py --mls ITECH        # testing
python3 update-status.py --dry-run          # preview
python3 update-status.py --sweep-only       # just relocate stuck-Closed docs
```

**API economics**: ~4,900 batched requests for ~125k docs vs. the old 125k individual requests. Full data refreshed on every call. Target runtime: ~90 min at ~3 req/sec.

### `fetch-photos.py`
Per-listing `GET /v1/listings/{key}/photos` → `media[]` inline on the doc. Spark's bulk `_expand=Photos` silently drops the expansion, so photos require per-listing queries.

- Iterates all three on-market collections (`unified_listings`, `unified_in_escrow`, `unified_off_market`) — photos preserved through the lifecycle before closing (Spark strips them after close).
- Writes updates back to the listing's current collection (not just unified_listings).
- Modes: default (no `photoCachedAt`), `--delta` (re-fetch if `modificationTimestamp > photoCachedAt`), `--force` (ignore cache marker).
- 1 worker, 0.5s sleep, 5-retry exponential backoff. Rate-limit-safe at ~2 req/sec.

```bash
python3 fetch-photos.py --mls GPS --limit 10    # sanity
python3 fetch-photos.py --all                    # full backfill (~24h)
python3 fetch-photos.py --all --delta            # daily cron
```

Spark returns photos for roughly ~30% of listings — the rest have permission-denied or empty responses (a Spark replication limitation, not a script bug). All three outcomes mark `photoCachedAt` so failures don't starve out successes on repeat runs.

### `cache-photos.py`
Caches **one primary photo per listing** into the separate `db.photos` collection keyed by `listingId`. Used by list/map/card views for O(1) primary-photo lookup without loading the full `media[]`. Runs across all three on-market collections. Uses `local-logs/photo-logs/skip_index.json` for permanent-403 resumability.

### `verify-db.py`
Read-only. Prints counts by MLS / status / propertyType, index list, and a sample document. Safe to run any time.

---

## Data model conventions

- **listingKey** — primary key; unique index in every lifecycle collection.
- **mlsSource** — `"GPS"` | `"CRMLS"` | `"CLAW"` | `"SOUTHLAND"` | `"HIGH_DESERT"` | `"BRIDGE"` | `"CONEJO_SIMI_MOORPARK"` | `"ITECH"`.
- **propertyType** — `A` (Residential), `B` (Residential Lease), `C` (Residential Income), `D` (Land), `E` (Commercial Sale), `F` (Commercial Lease), `G` (Business Opp), `H` (Manufactured), `I` (Mobile Home). CMA uses `A` only.
- **standardStatus** — see lifecycle table above. The status router reads this field exclusively.
- **coordinates** — GeoJSON `{type:"Point", coordinates:[lng,lat]}`, 2dsphere-indexed.
- **media[]** — camelCase photo objects: `uri300`/`uri640`/`uri800`/`uri1024`/`uri1280`/`uri1600`/`uri2048`/`uriThumb`/`uriLarge`, plus `caption`, `order`, `mediaCategory`, `imageWidth`, `imageHeight`.
- **lastSparkMissAt** + **sparkMissCount** — miss-escalation bookkeeping. Gets cleared on any successful Spark refresh.
- **statusLastChecked** — always set by `update-status.py`.
- **photoCachedAt** — set by `fetch-photos.py`; the `--delta` query looks at `modificationTimestamp > photoCachedAt`.

---

## Runtime environment

- Python 3.12 via `/root/jpsrealtor/.venv`
- Activated automatically on shell via `/root/.bashrc`
- Pinned deps: `/root/jpsrealtor/requirements.txt`
- Required env in `/root/jpsrealtor/.env.local`: `SPARK_ACCESS_TOKEN`, `MONGODB_URI`

```bash
# Install deps into the venv
/root/jpsrealtor/.venv/bin/pip install -r /root/jpsrealtor/requirements.txt
```

---

## Operational cheatsheet

**Check if tonight's cron ran:**
```bash
ls -la /var/log/mls-update.log /var/log/mls-status-update.log /var/log/cma/*.log
tail -30 /var/log/mls-status-update.log
```

**Live lifecycle counts:**
```bash
python3 -c "
import os; from pathlib import Path; from dotenv import load_dotenv; from pymongo import MongoClient
load_dotenv(Path('/root/jpsrealtor/.env.local'))
db = MongoClient(os.getenv('MONGODB_URI')).get_database()
for c in ('unified_listings','unified_in_escrow','unified_off_market','unified_closed_listings'):
    print(f'{c}: {db[c].count_documents({}):,}')
"
```

**Manual refresh for one MLS (ad-hoc):**
```bash
python3 run-pipeline.py --mls CRMLS --incremental
python3 update-status.py --mls CRMLS
```

**Reinstate indexes only (safe, no writes):**
```bash
python3 seed.py --indexes-only
```

---

## Known gotchas

- **BRIDGE / ITECH reject `_expand=Media`.** The script handles this automatically, but any new script that queries Spark with Media expansion needs the same fallback.
- **Spark doesn't return OffMarket listings.** That's why the miss-escalation safety net exists.
- **Photo coverage is ~30% per MLS** — Spark's replication feed doesn't expose photos for every listing. This is feed-level, not a pipeline bug.
- **`subdivisionName` ↔ `subdivisions.name` orphan rate.** Historically ~43% on Palm Springs before alias-based matching was added. The current CMA builder uses exact-case-insensitive matching only; see `src/scripts/cma/README.md`.
- **Legacy collections still present** (`listings`, `crmls_listings`, `gpsClosedListings`, `crmlsClosedListings`, `closed_listings`) — none are written by the unified pipeline anymore. Safe to archive when ready.
