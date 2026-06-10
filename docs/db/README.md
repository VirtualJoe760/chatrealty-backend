# jpsrealtor MongoDB — Database Documentation

Snapshot date: **2026-04-29**
Cluster: `jpsrealtor-mongodb-911080c1.mongo.ondigitalocean.com` (DigitalOcean Managed MongoDB)
Connection: `mongodb+srv://doadmin@.../admin?...`

This folder is the source of truth for what's in our database, what's used, what isn't, and what we need to fix before mass adoption.

## Read order

1. **[current-state.md](current-state.md)** — full inventory of every database, every collection, sizes, indexes, schema highlights, freshness, and which are actively written.
2. **[cleanup-and-migration.md](cleanup-and-migration.md)** — what to drop, what to consolidate, how to move off the `admin` database, and a mass-adoption readiness checklist.

## TL;DR

- **5 databases** exist on the cluster: `admin` (everything), `payload` (orphan), `your-db-name` (placeholder, drop), `config` and `local` (Mongo internals — leave alone).
- **`admin` holds all 70 application collections** (~16.5 GB of data, ~6.5 GB on disk, ~2.86M objects). This is wrong — `admin` is reserved for Mongo's auth/role system. Application data should live in a dedicated database such as `jpsrealtor`.
- We're running **two parallel listing pipelines** that both write today: the legacy per-MLS pipeline (`listings`, `crmls_listings`, `crmlsClosedListings`, `gpsClosedListings`, `closed_listings`, `photos`) and the unified pipeline (`unified_listings`, `unified_closed_listings`, `unified_in_escrow`, `unified_off_market`). The legacy collections are **fully duplicated inside the unified ones** for the MLSs that have been migrated.
- **~25 collections are empty** — many were created by Mongoose model registration on the Node/Payload side and never used. Safe to drop after the consuming code is removed/confirmed unused.
- **`unified_closed_listings` has 46 indexes (1.3 GB of index data)** and `unified_listings` has 62 indexes — heavy write amplification. At least a third look redundant.
- **`unifiedclosedlistings` and `unifiedlistings`** (no underscores) are MongoDB views that pass-through to the real `unified_*` collections. They exist purely so Mongoose's auto-pluralized model name resolves to the same data.

See `cleanup-and-migration.md` for prioritized actions.
