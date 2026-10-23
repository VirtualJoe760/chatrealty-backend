# Closed Listings Pipeline

**Status**: Production
**Coverage**: 7 of 8 MLS associations — ITECH excluded, see *MLS coverage* below
**Data Retention**: 5-year rolling window (backfill) · 30-day rolling delta (daily)
**Target Collection**: `unified_closed_listings` (~1.5M docs)

Feeds closed/sold listings into `unified_closed_listings`, which is the exclusive comp source for `build-subdivision-cma.py`.

---

## Directory

```
historical/
├── README.md                ← you are here
├── fetch.py                 ← Spark fetch for StandardStatus='Closed' in a time window
├── flatten.py               ← Closed-specific flatten (camelCase, closeDate coercion)
├── seed.py                  ← Bulk upsert into unified_closed_listings
├── sync.py                  ← chains fetch → flatten → seed per MLS
├── build-market-history.py  ← annual archive into market_history
├── rank-subdivisions.py     ← read-only report over market_history
├── build-area-geo.py        ← area_geo: where each area is
├── build-sales-grid.py      ← sales_grid: sales by coordinate, for heat maps
└── build-sales-hotspots.py ← sales_hotspots: clustered zones for the map
```

`sync.py` is the orchestrator for the daily rolling delta.
`build-market-history.py` runs in the other direction: it reads the collection
and writes permanent per-year aggregates, so the TTL cannot take the history
with it. See *The annual archive* below. `rank-subdivisions.py` only reads
that archive — see *Ranking subdivisions* below.

---

## MLS coverage

The closed sync covers **7** associations:

```
GPS · CRMLS · CLAW · SOUTHLAND · HIGH_DESERT · BRIDGE · CONEJO_SIMI_MOORPARK
```

### ITECH — excluded from closed sync only

**ITECH is still fully active in the on-market pipeline.** Its listings fetch,
flatten and seed normally through `unified/run-pipeline.py`, whose own
`MLS_OPTIONS` lists all 8. Only closed/sold sales are skipped here.

Its fetch returns HTTP success but writes no closed-sales file, which is what a
feed without closed/sold access for the association looks like — a real fetch
failure surfaces an HTTP error, and sparse data would leave a file that was
present but empty. The missing-file guard catches it and refuses to seed, so
the failure is loud instead of silently re-seeding stale comps.

ITECH is a very small association: ~165 KB of on-market data against CRMLS's
548 MB. Excluding its closed sales costs almost no comp coverage, and keeping it
in the list meant every run reported a failure that needed no action — noise
that would eventually get ignored and mask a real one.

Declared in `historical/sync.py` as `EXCLUDED_MLS`. It remains a valid
`--mls` / `--exclude` choice on purpose, so an explicit request prints the
reason instead of argparse's opaque "invalid choice":

```
$ historical/sync.py --mls ITECH
[SKIP] ITECH is excluded from the closed sync: no closed/sold feed access for
       this association
       ITECH is unaffected in the on-market pipeline (unified/run-pipeline.py).
```

Revisit if closed access is confirmed for this feed; removing the entry from
`EXCLUDED_MLS` restores it.

---

## Two operational modes

### 1. Daily delta (automatic — 10:00 cron)

`sync.py` runs a 30-day rolling window across all 8 MLSs. Cheap. Catches recently-closed listings including late corrections and price amendments.

```
0 10 * * *  /root/jpsrealtor/.venv/bin/python3 src/scripts/historical/sync.py
```

Log: `/var/log/mls-sync/historical-closed-sync.log`.

### 2. One-time 5-year backfill (manual)

Used when seeding the collection from scratch (e.g., after a DB migration) or when expanding to new MLSs.

```bash
# All 8 MLSs with prompts between each
python3 src/scripts/historical/fetch.py

# Auto-confirm
python3 src/scripts/historical/fetch.py -y

# Exclude slow MLSs
python3 src/scripts/historical/fetch.py --exclude GPS CRMLS -y

# Then flatten + seed
python3 src/scripts/historical/flatten.py
python3 src/scripts/historical/seed.py
```

Scope is ~1.5M closings across all MLSs going back 5 years. Full backfill takes several hours.

---

## Scripts

### `fetch.py`
- **Filter**: `StandardStatus Eq 'Closed'` + `CloseDate ge <now - years>`.
- `--years N` accepts **float** (e.g., `--years 0.0821` for a 30-day window from `historical/sync.py`).
- `--mls` / `--exclude` for scoping.
- `-y` for no-prompt batch mode (used by cron).
- `--delay` for rate-limit pacing (default 2.0 s/request).
- Writes `local-logs/closed/<MLS>_closed_listings.json`.

### `flatten.py`
Normalizes Spark payloads to camelCase. Note that `closeDate` leaves here as a string, because the stage writes JSON and JSON has no date type. `seed.py` is what turns it into a BSON date.

### `seed.py`
Bulk upserts into `unified_closed_listings` via `$set` on `listingKey`. Does not touch other lifecycle collections — closed docs live only here.

`parse_date` runs every `closeDate` through `ingest/closedate.py`, which is the
single rule shared with the live ingest writers. It returns `None` for an
unparseable value *and* for an implausible far-future one — the feed really
sends years like 4008 — and a `None` closeDate makes the row skipped.

Indexes created: `listingKey_unique`, `mlsSource_mlsId`, `subdivision_status`, `city_status`, `coordinates_2dsphere`, `modificationTimestamp_desc`, `closeDate_desc` (CMA sorts closings by this).

---

## Data shape notes

Closed docs mirror active-listing shape plus:

- **closePrice** — sale price
- **closeDate** — close of escrow. BSON date, always; query it with a `datetime`, never an ISO string, or the comparison matches nothing
- **originalListPrice** — used for sale-to-list ratio
- **listPrice** — fallback for sale-to-list if originalListPrice is missing
- **daysOnMarket** — when present, trusted; otherwise derive from `onMarketDate` → `closeDate`

The collection is **a superset** — contains not just sales but also lease closings (`propertyType='B'`) and anomalies (`closePrice` < $15 — likely $1 token transactions or data noise). CMA consumers must filter by propertyType/landType at read time.

---

## The annual archive

The TTL is the point of this section. `closeDate_ttl_5years` deletes any closing
older than five years, which is correct — Spark only serves five years back, so
the window is the feed's horizon, not a gap we could fill. But it means the
history is on a clock. Most of 2021 has already gone: the collection holds
three months of it (about 51k closings, against 209k for a full year like 2024),
and 2022 is next.

`build-market-history.py` reads the closings while we still hold them and writes
one permanent per-year aggregate per area into `market_history`, which has no
TTL. Five years of individual sales becomes an indefinite series of annual
medians — the only way a long-run appreciation estimate is possible at all,
since otherwise the view back would always stop five years short of today.

Scopes are `zip`, `city` and `subdivision`, and they overlap on purpose: a sale
in a named subdivision counts toward its ZIP and its city too. Both property
types are archived — `A` is sales, and on a `B` lease `closePrice` is the
monthly rent, so the same aggregate doubles as rent history.

```bash
# what the archive holds vs what the collection can still produce
python3 src/scripts/historical/build-market-history.py --status

# the normal run
python3 src/scripts/historical/build-market-history.py --all
```

### A re-run must never make the archive worse

Once the TTL eats part of a year, recomputing that year produces fewer closings
than the archived figure was built from. An ordinary upsert would replace a
complete 2022 with a gutted one, which is exactly the loss the archive exists to
prevent. So any write that would **lower** an existing `closedCount` is refused
and reported, and `--force` is the only way past it.

`partialYear` carries the same warning into the data: it is true when the
collection holds fewer than 12 months of that calendar year, or the year is
still running. In a market this seasonal the mix of months alone moves a median,
so a partial year must never be read as a full one.

It is a verdict on the **year**, not the area — `yearMonthsCovered` is what sets
it. The per-area `monthsCovered` is a separate field, and a subdivision with
closings in 7 months of a complete year is a thin area, not truncated data.
Tying the flag to the per-area count would mark most subdivision rows partial in
a year that is perfectly complete.

## Ranking subdivisions

`rank-subdivisions.py` answers "which subdivisions are selling the most" out of
`market_history`. It is a read-only operator report: every figure it prints is a
stored field, summed or sorted, so it never rescans the closings and never
defines a second median.

```bash
# busiest subdivisions in the latest complete archived year
python3 src/scripts/historical/rank-subdivisions.py

# six-year totals with the per-year breakdown
python3 src/scripts/historical/rank-subdivisions.py --all --trend

# one city, or leases instead of sales, or ranked by speed of sale
python3 src/scripts/historical/rank-subdivisions.py --city "Palm Desert"
python3 src/scripts/historical/rank-subdivisions.py --type B
python3 src/scripts/historical/rank-subdivisions.py --sort dom --min-count 50
```

### Placeholder names are excluded by default

About **31%** of closings that carry a `subdivisionName` carry a placeholder —
`Other`, `Not Applicable`, `N/A`. Those mean "none entered", not the name of a
large community, and ranked naively they take 8 of the top 10 slots: `Other` in
Corona alone logs 4,033 sales. The script drops them and reports how many it
dropped; `--include-placeholders` puts them back, marked.

The blocklist is the union of the sets already in `build-listing-cma.py`,
`build-search-index.py`, `build-closed-seasonal-rates.py` and
`build-cashflow-profile.py`, plus a pattern those four miss: Ventura and Santa
Barbara counties spell the placeholder as an MLS area code (`Ojai: Other`,
`Oxnard: Other - 0031`).

A subdivision whose name is simply its city (`Oceanside` in Oceanside, 4.2% of
closings) is kept but marked `*` — a real area, only a coarse one.

### Name coverage is printed, because it collapsed

CRMLS stopped publishing `SubdivisionName` through Spark around **September
2025** — Spark returns the field as `null`, and `camelize_keys` drops nulls, so
the key is absent. No substitute field survives either. Coverage on type-A
closings: 26.2% (2024) → 20.4% (2025) → **11.0% (2026)**, a cliff between Aug
and Sep 2025, not a drift. The Greater Palm Springs feed is unaffected, so
Coachella Valley is sound; CRMLS territory is not. Full detail in
`docs/tools/market-history.md`.

`rank-subdivisions.py` therefore prints coverage per year and warns below 20%,
deriving the ratio from the archive alone (city scope counts every closing with
a city, subdivision scope only the named ones) so it costs no extra scan.
Without that line a 2026 ranking looks exactly like a 2024 one while resting on
a tenth of the market.

### Counts sum across years; medians do not

The median of six annual medians is not a six-year median. A multi-year run
therefore sums `closedCount` but takes each median from the latest year **that
subdivision** has rows for, printed per row in the `yr` column. A blank cell in
the `--trend` table means no archived rows that year: either no sales, or fewer
than the 5 `build-market-history.py` needs to store an area.

## Mapping sales volume

`build-area-geo.py` writes `area_geo` (one centroid, bbox and p90 radius per
area, keyed exactly as `market_history` keys it). `build-sales-grid.py` writes
`sales_grid` (sales per ~0.3 mi cell, grouped by **coordinate** not by name,
with a `namedShare` on every cell).

The grid exists because most closings carry no usable subdivision name — 11% of
2026 type-A closings carry one at all, and 31% of those are placeholders like
`Other` — and grouping the rest by name collapses them into one per-city bucket
spanning miles. `build-sales-hotspots.py` then writes `sales_hotspots`:
clustered zones, one doc per zone per year, each with a centre, radius and a
caption decided at build time — clustering is a calculation, so it cannot live
in a read route.

`build-area-competition.py` writes `area_competition`: how many agents sold each
area and how concentrated they were. The headline is `effectiveAgents` (1/HHI,
the equal-sized competitors matching the concentration observed), not the raw
count, which agents passing through inflate. Sun City is the valley's
best-selling subdivision and no farm at 439 agents; Seven Lakes Country Club
sells an eighth as much with one agent on 71% of it and no new construction —
`vintage.newBuildShare` keeps that separate, because a tract whose sales are new
builds is held by whoever built it. They chain in this order:

```bash
python3 src/scripts/historical/build-area-geo.py --cv
python3 src/scripts/historical/build-sales-grid.py --cv
python3 src/scripts/historical/build-sales-hotspots.py --cv
python3 src/scripts/historical/build-area-competition.py --cv
```

None needs the shrink guard: all rebuild from the closings. Full detail — why
zones are peaks rather than contiguous blobs, why sentinel "non member" agents
must be excluded — is in `docs/tools/sales-heatmap.md` and
`docs/tools/area-competition.md`.

## Relationship to the rest of the pipeline

- `update-status.py` moves any on-market doc that transitions to `Closed` into this collection (router logic in `seed.py`, sweep in `update-status.py`).
- `historical/sync.py` is the backup: daily re-fetches the last 30 days of closings directly from Spark, so anything `update-status.py` missed gets picked up.
- `build-subdivision-cma.py` reads from here for its closed-comps aggregates.
- Photos are **not** fetched here — Spark strips media after close. The `media[]` that was preserved during the listing's on-market life stays with the doc when it's moved.
