# Tools — Precomputed Data Reference

**Audience:** frontend / MCP agents building AI-facing tools in the **chatrealty
frontend repo**. This backend repo computes and stores; your repo reads and
serves. These docs are the contract between the two.

**Read `docs/strategies/tool-development.md` first.** It explains why the data is
shaped this way and walks through the **four shapes of tool** — look up a
precomputed answer, list the options, rank by a stat, or write a record — each
with its own rules.

**Then read [how-to-call.md](how-to-call.md).** It is the implementation
contract: the one access mode (read MongoDB — **no script here is callable on
demand and there is no HTTP API**), the connection, the four unique lookup keys
and how to resolve a user's words into them, and a worked end-to-end example.
Every block doc below has a **How to get it** section with the exact query.

The short version: **the AI must never calculate.** Everything indexed below was
computed offline by a cron job; the tool does one `findOne` and returns the
block. Note that not every tool needs precomputing — listing the subdivisions in
a city is just an indexed query, and ranking tools mostly need an index plus
sanity guards. The strategy doc covers when you need a script here and when you
don't.

---

## Where everything lives

Database: **`admin`** (yes — all app data is in `admin`; migration to
`jpsrealtor` is tracked in `docs/db/cleanup-and-migration.md`. Use
`client.get_database()` / the default DB from the URI rather than hardcoding.)

| Collection | Docs | Keyed by | Holds |
|---|---:|---|---|
| `unified_listings` | 86,233 | `listingKey` (unique) | active listings; sale = `propertyType: "A"`, lease = `"B"` |
| `unified_closed_listings` | 1,112,323 | `listingKey` (unique) | closed sales + closed leases (`closePrice` = achieved rent on a lease) |
| `subdivisions` | 1,426 | `slug` | per-community rollups |
| `cities` | 1,047 | `slug` | per-city rollups |
| `rent_rates` | 1,037 | `postalCode` (unique) | per-ZIP rental rollups |
| `agents` | 240,236 | `_id` = `"<mlsSource>:<lower mlsId>"` | one doc per MLS agent registration |
| `brokerages` | 29,703 | `slug` | per-brokerage-brand rollups |
| `search_index` | 49,614 | — | search-suggest payloads |

## The block index

Every analytics output is a self-describing block on one of those documents.

Each doc's **How to get it** section carries the exact query, the key, what the
tool should return, and the absent case.

| Block | Lives on | Producer | Coverage | Doc |
|---|---|---|---:|---|
| `cashflowProfile` | `subdivisions`, `cities` | `build-cashflow-profile.py` | 741 subs / 836 cities | [cashflow-profile.md](cashflow-profile.md) |
| `cashflowStats` | `unified_listings` (sale) | `build-cashflow.py` | ~41.9k listings | [listing-cashflow.md](listing-cashflow.md) |
| `rentStats` | `subdivisions` | `build-going-rates.py` | 1,101 | [rent-rates.md](rent-rates.md) |
| *(whole doc)* | `rent_rates` | `build-going-rates.py` | 919 w/ `goingRate` | [rent-rates.md](rent-rates.md) |
| `rentStats.seasonal` | `subdivisions`, `rent_rates` | `build-seasonal-rates.py` | 459 subs / 763 ZIPs | [rent-rates.md](rent-rates.md) |
| `rentStats.closedSeasonal` | `subdivisions`, `rent_rates` | `build-closed-seasonal-rates.py` | 948 subs / 1,015 ZIPs | [rent-rates.md](rent-rates.md) |
| `rentStats.poolPremium` | `subdivisions`, `rent_rates` | `build-pool-premium.py` | 759 subs / 896 ZIPs | [rent-rates.md](rent-rates.md) |
| `inventoryProfile` | `subdivisions`, `cities` | `build-inventory-profile.py` | 742 subs / 836 cities | [inventory-profile.md](inventory-profile.md) |
| `cmaStats` | `subdivisions` | `build-subdivision-cma.py` | 1,426 | [cma-comps.md](cma-comps.md) |
| `cmaStats` | `unified_listings` | `build-listing-cma.py` | 46,768 listings | [cma-comps.md](cma-comps.md) |
| `rateProfile` | `unified_listings` (lease) | `build-rate-profile.py` | 15,935 of 15,936 leases | [rent-rates.md](rent-rates.md) |
| *(whole doc)* | **`agents`** | `agents/build-agents.py` | 240,236 agents | [agents.md](agents.md) |
| *(whole doc)* | **`brokerages`** | `agents/build-agents.py` | 29,719 brokerages | [agents.md](agents.md) |
| *(whole doc)* | **`agent_roster`** | `sync-roster.py` | 158,920 members | [agents.md](agents.md) |

> All coverage counts measured against production on **2026-10-14**. Most are
> not 100% and never will be — thin markets genuinely lack comps. **Always
> handle the absent case**; the blocks say why they are absent rather than
> guessing a number. Re-measure before relying on an exact figure.

## Refresh cadence

Data-refresh jobs run **twice a week at ~1–2 AM**, not daily. Rents and comps
move slowly; fidelity beats cadence. Every block carries `lastUpdated` — surface
it when a user asks how current a figure is.

| When | Chain |
|---|---|
| Daily 7 AM | `update-status.py` (status inference) |
| Daily 8 AM | `build-subdivision-cma.py --all` |
| Daily 4 AM | `build-search-index.py` |
| Sun 1 AM | `build-listing-cma.py --all` |
| Tue & Fri 3 AM | `build-agents.py --all` (~17 min) |
| Mon–Sat 9:30 AM | `build-rate-profile.py --all` |
| **Tue + Fri 1:30 AM** | `build-seasonal-rates` → `build-closed-seasonal-rates` |
| **Tue + Fri 2 AM** | `build-rent-rates` → `build-pool-premium` → `build-cashflow` → `build-inventory-profile` → `build-cashflow-profile` |

The 2 AM chain is strictly ordered and `&&`-chained so no stage reads a
half-built input. Full detail in `crontab.vps`.

---

## Four things that will bite you

**1. Mongoose `strict: true` silently drops these fields.** The Python builders
use PyMongo and bypass Mongoose entirely. Declare them or `.lean()` reads return
nothing:

```ts
// UnifiedListing schema
cashflowStats: { type: Schema.Types.Mixed },
cmaStats:      { type: Schema.Types.Mixed },
rateProfile:   { type: Schema.Types.Mixed },

// Subdivision schema
rentStats:        { type: Schema.Types.Mixed },
cmaStats:         { type: Schema.Types.Mixed },
inventoryProfile: { type: Schema.Types.Mixed },
cashflowProfile:  { type: Schema.Types.Mixed },

// City schema
inventoryProfile: { type: Schema.Types.Mixed },
cashflowProfile:  { type: Schema.Types.Mixed },

// rent_rates has no Mongoose model yet
const RentRate = model("RentRate",
  new Schema({}, { strict: false, collection: "rent_rates" }));
```

**2. ZIP-first, not subdivision-first.** Only ~13% of sale listings carry a
`subdivisionName`; ~99% carry a `postalCode`. Every area-level block falls back
subdivision → ZIP → absent and records which fired in a `source` field. Read
that field and tell the user when a figure came from the ZIP rather than their
community.

**3. Rate limits and sorts need indexes.** A filter+sort over 86k listings
collscans without them:

```js
db.unified_listings.createIndex(
  { city: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "city_cashflow20" });
db.unified_listings.createIndex(
  { postalCode: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "zip_cashflow20" });
```

**4. Never feed a peak rate into payment math.** See
[rent-rates.md](rent-rates.md#unit-discipline). This is a 2x error and it looks
plausible.

---

## Tool surface: what exists, what's missing

Existing chatRealty MCP tools that these blocks should back:
`get_subdivision_cma`, `analyze_listing_cashflow`, `find_cashflowing_listings`,
`get_going_rate`, `get_market_stats`, `find_comparables`.

Known gaps as of 2026-10-14 — data is shipped and scheduled, no tool reads it:

| Gap | Block to serve | Question it answers |
|---|---|---|
| **No area cash-flow tool** | `subdivisions/cities.cashflowProfile` | "What could cash-flow in Palm Desert Country Club?" — currently answerable only by scoring listings one at a time |
| No achieved-seasonal surface | `rentStats.closedSeasonal`, `cashflowStats.strategies.closedSeasonal` | "What do seasonal rentals actually get here?" (vs what listings ask) |
| No divergence surface | `cashflowProfile.rents.seasonalDivergence` | "Is the asking seasonal rate realistic?" |
| No inventory-mix tool | `inventoryProfile` | "How many 3/2s are for sale here, and do they have private pools?" |
| No pool-premium tool | `rentStats.poolPremium` | "What is a private pool worth in rent?" |

The first row is the highest-value one: `cashflowProfile` was built precisely so
that a whole-area cash-flow answer costs one `findOne`.

---

To wire the agent and brokerage collections into the frontend, hand
[frontend-agent-prompt.md](frontend-agent-prompt.md) to that repo's agent — it is a
self-contained spec of both document shapes, the indexes, and the nine ways to
get a wrong answer out of them.
