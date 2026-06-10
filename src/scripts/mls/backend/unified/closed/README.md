# Closed Listings Pipeline

**Last Updated**: 2026-04-22
**Status**: Production
**Coverage**: All 8 MLS associations
**Data Retention**: 5-year rolling window (backfill) · 30-day rolling delta (daily)
**Target Collection**: `unified_closed_listings` (~1.5M docs)

Feeds closed/sold listings into `unified_closed_listings`, which is the exclusive comp source for `build-subdivision-cma.py`.

---

## Directory

```
unified/closed/
├── README.md              ← you are here
├── fetch.py               ← Spark fetch for StandardStatus='Closed' in a time window
├── flatten.py             ← Closed-specific flatten (camelCase, closeDate coercion)
└── seed.py                ← Bulk upsert into unified_closed_listings
```

The root-level orchestrator `backend/historical_closed_sync.py` chains `fetch → flatten → seed` per MLS for the daily rolling delta.

---

## Two operational modes

### 1. Daily delta (automatic — 10:00 cron)

`backend/historical_closed_sync.py` runs a 30-day rolling window across all 8 MLSs. Cheap. Catches recently-closed listings including late corrections and price amendments.

```
0 10 * * *  /root/jpsrealtor/.venv/bin/python3 src/scripts/mls/backend/historical_closed_sync.py
```

Log: `/var/log/mls-sync/historical-closed-sync.log`.

### 2. One-time 5-year backfill (manual)

Used when seeding the collection from scratch (e.g., after a DB migration) or when expanding to new MLSs.

```bash
# All 8 MLSs with prompts between each
python3 src/scripts/mls/backend/unified/closed/fetch.py

# Auto-confirm
python3 src/scripts/mls/backend/unified/closed/fetch.py -y

# Exclude slow MLSs
python3 src/scripts/mls/backend/unified/closed/fetch.py --exclude GPS CRMLS -y

# Then flatten + seed
python3 src/scripts/mls/backend/unified/closed/flatten.py
python3 src/scripts/mls/backend/unified/closed/seed.py
```

Scope is ~1.5M closings across all MLSs going back 5 years. Full backfill takes several hours.

---

## Scripts

### `fetch.py`
- **Filter**: `StandardStatus Eq 'Closed'` + `CloseDate ge <now - years>`.
- `--years N` accepts **float** (e.g., `--years 0.0821` for a 30-day window from `historical_closed_sync.py`).
- `--mls` / `--exclude` for scoping.
- `-y` for no-prompt batch mode (used by cron).
- `--delay` for rate-limit pacing (default 2.0 s/request).
- Writes `local-logs/closed/<MLS>_closed_listings.json`.

### `flatten.py`
Normalizes Spark payloads to camelCase. Key concern: `closeDate` comes back mixed-typed (sometimes `datetime`, sometimes ISO string). The CMA builder's `coerce_date` helper handles both forms downstream, but flatten normalizes where it can.

### `seed.py`
Bulk upserts into `unified_closed_listings` via `$set` on `listingKey`. Does not touch other lifecycle collections — closed docs live only here.

Indexes created: `listingKey_unique`, `mlsSource_mlsId`, `subdivision_status`, `city_status`, `coordinates_2dsphere`, `modificationTimestamp_desc`, `closeDate_desc` (CMA sorts closings by this).

---

## Data shape notes

Closed docs mirror active-listing shape plus:

- **closePrice** — sale price
- **closeDate** — close of escrow (may be string or date; coerce in consumers)
- **originalListPrice** — used for sale-to-list ratio
- **listPrice** — fallback for sale-to-list if originalListPrice is missing
- **daysOnMarket** — when present, trusted; otherwise derive from `onMarketDate` → `closeDate`

The collection is **a superset** — contains not just sales but also lease closings (`propertyType='B'`) and anomalies (`closePrice` < $15 — likely $1 token transactions or data noise). CMA consumers must filter by propertyType/landType at read time.

---

## Relationship to the rest of the pipeline

- `update-status.py` moves any on-market doc that transitions to `Closed` into this collection (router logic in `seed.py`, sweep in `update-status.py`).
- `historical_closed_sync.py` is the backup: daily re-fetches the last 30 days of closings directly from Spark, so anything `update-status.py` missed gets picked up.
- `build-subdivision-cma.py` reads from here for its closed-comps aggregates.
- Photos are **not** fetched here — Spark strips media after close. The `media[]` that was preserved during the listing's on-market life stays with the doc when it's moved.
