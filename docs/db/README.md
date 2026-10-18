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

Derived aggregates either get their own collection (`rent_rates`) or ride as a
subdocument on what they describe — `cmaStats` and `cashflowProfile` on
`subdivisions` and `cities`, `cashflowStats` and `media[]` on listings. Which
script writes each one is in `AGENTS.md`; the schedule is `crontab.vps`.

The live cluster is the source of truth for counts, sizes and index lists. Query
it rather than trusting a number written in a doc.

## Migration planning

- **[current-state.md](current-state.md)** — hand-maintained collection
  inventory with sizes, indexes and freshness.
- **[cleanup-and-migration.md](cleanup-and-migration.md)** — what to drop and
  consolidate, and moving application data off `admin`.
- **[vps-migration-plan.md](vps-migration-plan.md)** — host migration.

These describe intended future work, not the system as it runs.
