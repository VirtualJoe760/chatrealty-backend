# Cleanup, Consolidation & Mass-Adoption Readiness

This document is the action plan that flows from `current-state.md`. **No changes have been applied yet** — this is documentation for the cleanup, not a record of work done.

Order matters. The first item gates several others.

---

## 0 · Read this first: blast radius

We have one production cluster, 16 GB of data, and code we don't fully control writing to it (the Next.js app is a separate deploy). Before any drop or rename:

1. **Take a fresh DigitalOcean managed-MongoDB backup.** DO Managed MongoDB takes daily snapshots; trigger a manual one before destructive work.
2. **Rename, don't drop.** For any collection slated for removal, `renameCollection` it to `_attic_<name>_<date>` first and watch for application errors for at least one full sync cycle (24h covers daily crons; one week covers weekly reports). Drop the attic only after the soak window.
3. **Search the Next.js codebase.** Most of the unknowns in this doc come from not having visibility into the web app. Every "unused?" judgment below should be verified with a `grep -rn '"<collection>"'` and `mongoose.model('<Name>'` sweep through the frontend repo.

---

## 1 · Find the second writer (highest priority)

`crmls_listings`, `crmlsClosedListings`, `gpsClosedListings`, `listings`, and `photos` all received writes today — but the only cron on this VPS that hits MLS data is the unified pipeline, which writes `unified_*` collections. Something else is writing the per-MLS legacy collections.

**Until the second writer is identified, do not drop the legacy listing collections.** Likely culprits:

- An older Node/Express MLS sync still running on the web host.
- A scheduled function in the Next.js deploy.
- An `archive/` script wired into a cron we haven't seen (e.g., on another VPS).
- The Next.js application code reading and writing through Mongoose models with auto-pluralization (`Listing` → `listings`, `Photo` → `photos`).

Action: `grep -rn 'crmls_listings\|crmlsClosedListings\|gpsClosedListings' <web-app-repo>` and `grep -rn "mongoose.model('Listing'\|mongoose.model('Photo'" <web-app-repo>`. Audit any external cron servers.

---

## 2 · Drop the obvious junk (low risk)

These are safe to drop immediately after a backup:

| Item | Why | Action |
|---|---|---|
| Database `your-db-name` | 0 docs across 3 collections; created by misconfigured connection string | `db.getSiblingDB('your-db-name').dropDatabase()` after grepping the org for `your-db-name` to find/fix the offending config |
| Database `payload` | 11 collections, 3 docs total, leftover from abandoned Payload CMS install | Verify the 3 `payload.users` records aren't in use, then drop the database |
| `admin.crmlsclosedlistings` (lowercase, empty) | Mongoose-pluralized shadow of `crmlsClosedListings`; never received data | Drop |
| `admin.gpsclosedlistings` (lowercase, empty) | Same | Drop |
| `admin.temp_flattened` (3,198 docs, last write 2025-11-11) | Scratch table from a one-off flatten run | Drop |
| `admin.unifiedlistings_deprecated_2025_12_24` | Name says it all | Drop after a `grep deprecated_2025_12_24` sweep |
| `admin.runwaytasks` (19 docs, last write 2025-06-17) | 10-month-old image-gen jobs | Drop unless someone wants to inspect them |

## 3 · Consolidate duplicate listing data

Once step 1 confirms the unified pipeline is the only writer (or the legacy writer is decommissioned):

| Legacy collection | Status | Action |
|---|---|---|
| `crmls_listings` (104K, 680 MB) | CRMLS active+offmarket — duplicates `unified_listings`+`unified_off_market` for CRMLS | Verify rowcount/key parity, then rename to `_attic`, soak, drop |
| `crmlsClosedListings` (78K, 499 MB) | Strict subset of `unified_closed_listings` (CRMLS) — verified by sampling | Same pattern |
| `gpsClosedListings` (53K, 361 MB) | Strict subset of `unified_closed_listings` (GPS) — verified | Same |
| `closed_listings` (28K, 238 MB) | Older multi-MLS closed cache, last write 2026-04-13 | Same — confirm it's not the canonical source for any frontend page |
| `listings` (10K, 73 MB) | All `mlsSource: null` — pre-MLS-source-tagging legacy | Decide: salvage (reconcile `mlsSource`, merge into `unified_*`) or drop |

Estimated reclaim: **~1.85 GB** of data and the index overhead they carry.

## 4 · Decide on the `unifiedlistings` / `unifiedclosedlistings` views

These two are **MongoDB views** — they exist purely so a Mongoose model named `UnifiedListing` (auto-pluralized to `unifiedlistings`) and `UnifiedClosedListing` (auto-pluralized to `unifiedclosedlistings`) can resolve. They cost nothing at rest but every read goes through an aggregation pipeline (`[{$match: {}}]`).

**Two clean options:**

- **Drop the views and force the Node side to use `collection: 'unified_listings'`** explicitly in each Mongoose schema. One-line schema fix per model, then no view layer.
- **Or rename the underlying collections to `unifiedlistings` / `unifiedclosedlistings`** to drop the views, and fix the Python pipeline to use the new names. More disruptive — touches every Python script.

Recommend the first.

## 5 · Drop the empty collections

Empty in `admin` today, no recent writes — most are Mongoose model registrations that were never used or were superseded:

`agentsubscriptions`, `blog-posts`, `chat_messages`, `domain_mappings`, `generationsessions`, `listingviews`, `media`, `neighborhoods`, `openhouses`, `partnerships`, `payload-kvs`, `payload-locked-documents`, `payload-migrations`, `payload-preferences`, `pointsledgers`, `schools`, `searchactivities`, `twofactortokens`, `userpreferences`, `usersessions`, `verificationtokens`, `voicemailscripts`.

**Caveat:** an empty collection that exists today will be re-created automatically the next time the Mongoose model is touched. So dropping these is harmless but only sticks if the model definitions are also removed from the codebase. Treat this as a sweep: for each, `grep -rn 'model('<Name>'` first; if there are no live readers/writers, delete both the model file and the collection.

## 6 · Reduce the index count on `unified_*`

`unified_closed_listings` has 46 indexes (1.3 GB of index data). Several are duplicates of compound prefixes. Suggested first pass to remove (always with `db.collection.aggregate([{$indexStats:{}}])` first, to confirm zero usage over a week):

- Drop `coordinates_2dsphere` (the non-GeoJSON path) — keep `coordinates.coordinates_2dsphere`.
- Drop single-field indexes already covered as a prefix of a compound:
  - `city_1` (covered by `city_closeDate`)
  - `subdivisionName_1` (covered by `subdivisionName_closeDate_desc`)
  - `propertyType_1` (covered by `propertyType_closeDate`)
  - `mlsSource_1` (covered by `mlsSource_closeDate`)
  - `closePrice_1` (covered by `closePrice_closeDate`)
- Reconcile the `closeDate_ttl_5years` index: confirm whether it was meant to be a TTL (`expireAfterSeconds`). It currently is not; either set the TTL or rename it to remove the misleading name.
- Keep one index per compound prefix shape; drop `*_1_*_1` variants that duplicate `*_1` ascending forms.

For `unified_listings` (62 indexes), pick one of the duplicate field-name pairs and stick with it:
- `bedsTotal` vs `bedroomsTotal` — drop one set.
- `bathsTotal` vs `bathroomsTotalInteger` — drop one set.

This requires aligning the application code to whichever field name wins.

## 7 · Migrate application data out of the `admin` database

This is the structural fix. `admin` is reserved by MongoDB for authentication/authorization (it owns `system.users`, `system.roles`, `system.keys`). Putting application collections there is technically allowed but:

- Anyone with `dbAdmin@admin` to manage app collections also has cluster-admin-adjacent privileges. Least-privilege roles are awkward.
- Backups, restores, and per-database operations (e.g., `db.dropDatabase()`) carry extra risk.
- Tools and dashboards default to hiding `admin` — observability suffers.

### Why we ended up here

Every script in this repo connects with `client.get_database()` (no DB name), and the URI is `…ondigitalocean.com/admin?…`. The default DB in the URI is `admin`, so all writes land there. The Node side likely has the same config.

### Migration plan

**Phase A — make the move reversible**

1. Pick the new DB name. Recommend `jpsrealtor`.
2. Update the Mongo URI in every environment to `…ondigitalocean.com/jpsrealtor?authSource=admin&…`. The `authSource=admin` is critical — the user `doadmin` lives in `admin` but you can connect against any DB.
3. Update `.env.local` (already has `MONGODB_DB=admin`; change to `jpsrealtor`) and any equivalent in the web app, plus any scripts that read `MONGODB_DB`.
4. Add a startup assertion: `assert client.get_default_database().name == "jpsrealtor"` so a missed config raises loudly.

**Phase B — copy the data**

For each non-empty collection listed in `current-state.md` (skipping the ones marked for deletion in steps 2–3 of this doc):

```
mongodump --uri="...mongodb.com/admin?..." --collection=unified_listings --out=/tmp/mig
mongorestore --uri="...mongodb.com/jpsrealtor?..." --nsFrom='admin.*' --nsTo='jpsrealtor.*' /tmp/mig
```

Or all at once with a single dump/restore using `--nsFrom='admin.*' --nsTo='jpsrealtor.*'`. Total transfer is ~6.5 GB on disk; plan for a maintenance window.

**Phase C — switch over**

1. Stop writers (Python crons, web app workers).
2. Run a final delta `mongodump`/`mongorestore` for collections that took writes during the bulk copy.
3. Flip the URI / `MONGODB_DB`.
4. Start writers, watch logs.
5. Leave the old `admin.*` collections in place for a soak period (1–2 weeks). Then drop them.

**Phase D — restrict the `admin` DB**

Once empty, audit DigitalOcean's MongoDB users and ensure the application user (`doadmin` is the cluster admin — fine) has scoped privileges if a less-privileged service user is created.

---

## 8 · Mass-adoption readiness checklist

Things that bite at scale that we should fix before opening the doors:

- [ ] **Step 7 done** — application data is in `jpsrealtor`, not `admin`.
- [ ] **Step 1 resolved** — only one writer per collection. No phantom legacy pipelines.
- [ ] **Step 6 done** — indexes pruned on `unified_listings` and `unified_closed_listings`. Each remaining index has documented usage from `$indexStats`.
- [ ] **Sharding decision recorded.** At today's size (`unified_closed_listings` is 9.7 GB, 1.5M docs, growing) we don't need sharding yet. Document the trigger threshold (e.g., 50 GB or sustained write throughput >5k ops/s) so we don't get caught flat-footed.
- [ ] **Connection pool sizing.** Verify the Node app's `maxPoolSize` and the Python scripts' connection counts can sustain the cron schedule without blowing the cluster's connection cap. DO Managed MongoDB has a fixed limit per tier.
- [ ] **Replica reads where appropriate.** Heavy read endpoints (map search, listing detail) should use `readPreference=secondaryPreferred` to offload the primary.
- [ ] **TTL on `unified_closed_listings`.** Decide a retention window (5 years? 7?) and either set the TTL on the existing `closeDate` index or document that we keep all closed history forever.
- [ ] **Compound-key uniqueness on listings.** Today `listingKey_unique` is the only uniqueness guarantee on `unified_listings` and `unified_closed_listings`. Consider also `(mlsSource, mlsId)` unique to catch cross-MLS bugs.
- [ ] **`photos` join correctness.** Photos use `listingKey` and `listingId`; listings use `listingKey` and `id`. Pick one join key, ensure it's indexed on both sides, and remove the dead one.
- [ ] **`location_index` freshness.** Last update 2026-01-02; if this powers search-suggest UI it's stale by ~4 months. Schedule a rebuild or wire it into the unified pipeline.
- [ ] **Backup restore tested.** Pick a non-prod cluster, restore the latest snapshot, run a smoke test. Don't find out at 2 AM that the backup is broken.
- [ ] **Field-level encryption / PII review.** `contacts.phone`, `contacts.email`, `contacts.address`, and FUB-synced fields are PII. Depending on jurisdiction and partner agreements, evaluate whether MongoDB CSFLE or app-level encryption is warranted before opening to many agents.
- [ ] **Tenant isolation model.** With many agents on one cluster, decide between (a) `userId`/`teamId` filtering at the query layer (current), (b) per-tenant database, or (c) row-level access via MongoDB views per tenant. Document the chosen model.
- [ ] **Slow-query log enabled.** Turn on profiling at threshold ≥100 ms in DO's dashboard, route to a dashboard. You want to catch missing-index regressions the moment they ship.

---

## Quick numerical summary of the cleanup opportunity

| Action | Storage reclaimed | Index storage reclaimed | Risk |
|---|---:|---:|---|
| Drop `your-db-name` DB | 0 | 0 | None |
| Drop `payload` DB | < 1 MB | < 1 MB | Verify 3 users first |
| Drop empty/lowercase shadow collections | 0 | ~1 MB total | None after Mongoose model removal |
| Drop `temp_flattened` | 22 MB | < 1 MB | None |
| Drop `unifiedlistings_deprecated_2025_12_24` | 30 MB | 1 MB | grep first |
| Drop `runwaytasks` | trivial | trivial | None |
| Consolidate legacy listing collections (steps 1+3) | ~1,850 MB | ~40 MB | **High — gated on identifying the second writer** |
| Index audit on `unified_*` | n/a | est. 200–400 MB | Medium — drop after `$indexStats` confirms zero hits |
| Move out of `admin` (step 7) | 0 (it's a move) | 0 | Medium — staged migration |

Total disk reclaimed if all cleanup lands: roughly **2 GB** of collection data plus several hundred MB of index data, plus the ongoing write-amplification savings from fewer indexes and fewer parallel writes.
