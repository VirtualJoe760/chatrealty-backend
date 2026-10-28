# Database

MongoDB on DigitalOcean Managed MongoDB. One database, `admin`, holds every
application collection.

## Read order

1. **[operations.md](operations.md)** — the constraints that bite: no backups,
   one cluster with two names, dormant legacy collections. Read before any
   destructive work.
2. **[photos.md](photos.md)** — where photos live, and the `photoCachedAt` trap
   that hides photoless listings from the fetcher.

## Canonical collections

Listings live in the `unified_*` family, one collection per lifecycle stage:

| Collection | Holds |
|---|---|
| `unified_listings` | active + coming-soon |
| `unified_in_escrow` | pending sale |
| `unified_closed_listings` | closed sales |
| `unified_off_market` | expired, withdrawn, cancelled |

Geography reference data is `cities`, `counties`, `regions` and `subdivisions`.
The agent database is `agents`, `brokerages` and `agent_roster`.

Derived aggregates either get their own collection (`rent_rates`,
`market_history`) or ride as a subdocument on what they describe — `cmaStats`
and `cashflowProfile` on `subdivisions` and `cities`, `cashflowStats` and
`media[]` on listings. Which script writes each one is in `AGENTS.md`; the
schedule is `crontab.vps`.

Two collections are not derived from the current feed and must not be rebuilt
from it casually:

| Collection | Holds | Why it is special |
|---|---|---|
| `market_history` | annual medians per area | permanent; the live closings behind it expire on a 5-year TTL, so once a year is gone it cannot be recomputed |
| `loan_products` | down-payment ladder, mortgage-insurance costs | hand-tunable lender-pricing assumptions, not computed from anything |

Both are documented in `docs/tools/market-history.md`. `system_config` holds the
same kind of stored assumption for the investor rate spread.

Five more collections make sales volume, competition and comps mappable. All
five rebuild from the feed, so none needs a shrink guard:

| Collection | Holds |
|---|---|
| `area_geo` | centroid, bbox and p90 radius per area, keyed like `market_history` |
| `sales_grid` | sales per ~0.3 mi cell, grouped by coordinate so unnamed sales still map |
| `sales_hotspots` | clustered zones per year, each with a caption decided at build time |
| `area_competition` | how many agents sell in an area and how concentrated they are |
| `cma_comps` | one normalised comparable per document, closed and active together |

`cma_comps` is the only one keyed by listing rather than by area, because a
parcel drawn on a map spans both listing collections and a read-time join is
what the invariant exists to prevent. It is also where the listing CMA finds
each listing's nearest comps. Its `center` is built from the scalar
`latitude`/`longitude` pair, which is what makes `$geoWithin` complete —
`unified_closed_listings.coordinates` is 2dsphere indexed but present on a
minority of rows, so a geo query against the feed silently under-returns.

See `docs/tools/sales-heatmap.md`, `docs/tools/area-competition.md` and
`docs/tools/area-cma.md`.

The live cluster is the source of truth for counts, sizes and index lists. Query
it rather than trusting a number written in a doc.

## Migration planning

- **[current-state.md](current-state.md)** — hand-maintained collection
  inventory with sizes, indexes and freshness.
- **[cleanup-and-migration.md](cleanup-and-migration.md)** — what to drop and
  consolidate, and moving application data off `admin`.
- **[vps-migration-plan.md](vps-migration-plan.md)** — host migration.

These describe intended future work, not the system as it runs.
