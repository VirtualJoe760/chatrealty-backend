# Scripts

Every script here is a precompute stage. None of them answer a question; they
write a field or a collection that something else reads with a `findOne` or a
filtered `find`. If an answer needs arithmetic at request time, the arithmetic
belongs in a stage on this page.

Run everything through the venv from the repo root:

```
cd /root/jpsrealtor && .venv/bin/python3 src/scripts/<path>.py --all
```

One folder per domain, one level deep:

```
src/scripts/
├── rental/      6  what things rent for
├── investor/    3  whether it is a good buy
├── comps/       2  what things sell for
├── ingest/      9  live MLS feed -> unified_*
├── historical/  4  five years of closed sales
├── agents/      2  agent + brokerage database
├── search/      1  chat autocomplete index
└── utilities/  12  hand-run tools and spent one-shots
```

Every script sits exactly one level under `src/scripts/`, which is how each one
finds `.env.local` at `parents[3]`. A script nested deeper silently fails to
load the environment. Each folder has its own README.

`crontab.vps` is the schedule of record. Most stages take `--all`, a scope flag
(`--city`, `--zip`, `--slug`, `--key`), `--limit`, `--dry-run` and `--verbose`.

## The analytics chain — `rental/` then `investor/`

This is the stack that answers "what does this property earn and does it
cash-flow". Each stage reads what the stage above it wrote, so order matters.

| # | Script | Writes | Runs |
|---|---|---|---|
| 1 | `rental/build-rate-profile.py` | `rateProfile` on each lease listing | Mon-Sat 9:30 AM |
| 2 | `rental/build-going-rates.py` | `rentStats` on subdivisions, `rent_rates` per ZIP | Tue + Fri 2:00 AM |
| 3 | `rental/build-seasonal-rates.py` | `rentStats.seasonal` | Tue + Fri 1:30 AM |
| 4 | `rental/build-closed-seasonal-rates.py` | `rentStats.closedSeasonal` | Tue + Fri 1:30 AM |
| 5 | `rental/build-pool-premium.py` | `rentStats.poolPremium` | Tue + Fri 2:00 AM |
| 6 | `investor/build-cashflow.py` | `cashflowStats` on each sale listing | Tue + Fri 2:00 AM |
| 7 | `investor/build-inventory-profile.py` | `inventoryProfile` on subdivisions + cities | Tue + Fri 2:00 AM |
| 8 | `investor/build-cashflow-profile.py` | `cashflowProfile` on subdivisions + cities | Tue + Fri 2:00 AM |

The seasonal pair runs at 1:30 so both finish before the 2:00 chain, which reads
their output. Stages 2, 5, 6, 7 and 8 are a single `&&` chain — if one fails the
rest do not run, which is deliberate: a half-built chain would leave a rollup
disagreeing with the listings it summarizes.

**What each one is for, in a sentence:**

- **`build-rate-profile.py`** — reads every active lease listing and works out
  what it charges per season, because MLS lease listings bury tiered rates in
  free text rather than in fields.
- **`build-going-rates.py`** — the long-term going rate. Medians the annual-lease
  asking rents per subdivision and per ZIP, bedroom by bedroom.
- **`build-seasonal-rates.py`** — rolls the per-listing tiers from stage 1 into
  an area seasonal rate. This is what furnished listings **ask** for peak months.
- **`build-closed-seasonal-rates.py`** — the same figure derived from leases
  that actually **closed**. Asking and achieved routinely disagree, so both are
  kept and neither overrides the other.
- **`build-pool-premium.py`** — what a private pool is worth in monthly rent,
  per subdivision and per ZIP.
- **`build-cashflow.py`** — the calculator. Per sale listing, estimates rent,
  runs the financing and operating math, writes scenarios and the break-even
  down payment.
- **`build-inventory-profile.py`** — bedroom mix and pool prevalence for the
  for-sale inventory, which a cash-flow answer needs to describe an area.
- **`build-cashflow-profile.py`** — the rollup. Medians the per-listing results
  into one document per subdivision and city, so an area answer is one
  `findOne` by slug. It computes no new economics, which is why a rollup can
  never contradict the listing a user drills into.

### Three rental strategies, scored separately

`build-cashflow.py` runs its math three times, because these are different
businesses with different cost structures:

| Strategy | Rent basis | Management | Vacancy | Maintenance |
|---|---|---|---:|---:|
| Long-term | annual lease median | 8% | 5% | 5% |
| Seasonal (asking) | tiered rates annualized over 12 months | 20% | 15% | 8% |
| Seasonal (achieved) | closed peak + long-term off-peak, annualized | 20% | 15% | 8% |
| Short-term (nightly) | not modelled — no MLS source exists | — | — | — |

Seasonal income is always the annualized figure, never the peak rate. Putting a
peak rate into a payment calculation overstates income by two times or more.
Short-term is reported as unavailable with a reason rather than guessed.

## Where the money math lives — `investor/build-cashflow.py`

There is no separate mortgage calculator. All of it is in
`investor/build-cashflow.py`:

- **`fetch_current_mortgage_rate()`** — the live rate. Pulls the Freddie Mac
  30-year fixed (`frm_30`) from API Ninjas once per run, rejects anything
  outside 2-20%, and caches it to `system_config` under `_id:
  "mortgage_rate"`. On any failure it falls back to the last stored live rate,
  then to a 7% default, and records which in `assumptions.rateSource`. A
  rate-fetch failure never aborts a build.
- **`monthly_pi()`** — standard amortized monthly principal and interest.
- **`loan_from_pi()`** — the inverse: the largest loan whose payment is a given
  figure.
- **`break_even_down()`** — the smallest down payment at which monthly cash flow
  reaches zero. Uses the inverse above to solve it directly instead of
  iterating. Returns nothing when operating costs alone already exceed rent,
  which means a cash buyer would still lose money monthly — a different answer
  from "needs more down".
- **`monthly_hoa()`** — normalizes `associationFee` to monthly. The frequency
  field is about half populated, so a blank one is treated as monthly.

Stored defaults, all overridable on the command line and all recorded in
`cashflowStats.assumptions`: 30-year term, property tax 1.25% of price,
insurance 0.4% of price, closing costs 3% for the cash-on-cash denominator, and
down-payment scenarios at 20% and 25%.

Property tax is derived from list price because the MLS carries no tax amount.
Mello-Roos and special assessments are not modelled.

## Comparables — `comps/`

| Script | Writes | Runs |
|---|---|---|
| `comps/build-subdivision-cma.py` | `cmaStats` on subdivisions | daily 8:00 AM |
| `comps/build-listing-cma.py` | `cmaStats` on each listing | Sundays 1:00 AM |

Both write a field called `cmaStats`, on different collections — the
subdivision one is an area aggregate, the listing one is that listing's own comp
set. The listing job fans out over the whole active set, which is why it is
weekly and takes `--workers`.

## Ingest — `ingest/` and `historical/`

Pulls 8 MLS feeds through Spark into the canonical `unified_*` collections.

| Script | Purpose | Runs |
|---|---|---|
| `ingest/run-pipeline.py` | orchestrates fetch → flatten → seed | Sun 3 AM full, Mon-Sat 6 AM incremental |
| `ingest/fetch.py` | fetches listings from Spark | via pipeline |
| `ingest/flatten.py` | normalizes feed payloads to camelCase | via pipeline |
| `ingest/seed.py` | routes each listing to the right lifecycle collection | via pipeline |
| `ingest/update-status.py` | the daily freshener; walks every key and re-routes movers | daily 7 AM |
| `ingest/infer-listing-status.py` | decides expired vs likely-closed for listings that vanish from the feed | on demand |
| `ingest/fetch-photos.py` | writes `media[]` from Spark's photo sub-resource | daily 9:30 AM |
| `historical/{fetch,flatten,seed}.py` | the same three steps for 5 years of closed sales | via historical sync |
| `historical/sync.py` | runs the closed pipeline over a rolling window | daily 10 AM |
| `search/build-search-index.py` | rebuilds `search_index` for chat autocomplete | daily 4 AM |
| `ingest/verify-db.py` | integrity, index and MLS-distribution check after seeding | on demand |

See `docs/db/photos.md` for the `photoCachedAt` trap before touching photos.

## Agent database — `agents/`

| Script | Writes | Runs |
|---|---|---|
| `agents/sync-roster.py` | `agent_roster` from Spark member accounts | Tue + Fri 3 AM |
| `agents/build-agents.py` | `agents` and `brokerages` | Tue + Fri 3 AM |

Chained in that order: the roster supplies contact detail that listing data does
not carry. Served by `src/mcp/agents_server.py`.

## Read-only and one-shot — `utilities/`

- **`rental/audit-rate-tiers.py`** — classifies lease listings by how their rate
  tiers were extracted. Diagnostic; writes nothing.
- **`utilities/`** — counts, patches, purges and fetch probes for
  hand-running. `purge-stale.py` is safety-guarded; the rest are unguarded and
  mostly undocumented, so read one before running it.
- **`utilities/export-photos-collection.py`** and
  **`migrate-photos-to-media.py`** — spent one-shots from the photo migration.
- **`utilities/normalize-close-dates.py`** — spent one-shot that made
  `unified_closed_listings.closeDate` single-typed. Dry-run by default; takes
  `--apply`. Re-running it is the check that no writer has regressed, because
  `ingest/closedate.py` now normalizes the field on every write.
- **`archive/mls/backend/`** — the retired per-MLS pipeline. Superseded by
  `unified/`; kept for reference, not runnable as-is.

## Deeper detail

- `docs/tools/README.md` — the same system organized by output block, for tool
  authors
- `docs/cma/RENTAL_CASHFLOW.md` — the rental stack in full
- `docs/cma/SUBDIVISION_AND_CITY_CMA.md`, `docs/cma/LISTING_CMA.md` — comp
  builders
- `docs/tools/cashflow-profile.md`, `docs/tools/listing-cashflow.md` — output
  shapes
- `src/scripts/rental/README.md` — per-script notes next to the code
