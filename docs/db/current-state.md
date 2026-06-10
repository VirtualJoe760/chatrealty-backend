# Database — Current State

Snapshot taken **2026-04-29** against the production cluster.

## Cluster overview

| Database | Collections | Data size | Storage size | Objects | Status |
|---|---:|---:|---:|---:|---|
| `admin` | 70 | 16,455.6 MB | 6,460.6 MB | 2,861,750 | **All app data lives here. Should be migrated.** |
| `payload` | 11 | < 1 MB | 0.1 MB | 3 | Orphan from Payload CMS install. Drop. |
| `your-db-name` | 3 | 0 | 0 | 0 | Placeholder created by misconfigured tool. Drop. |
| `config` | 8 | 0 MB | 0.2 MB | 46 | Mongo internal. Leave alone. |
| `local` | 11 | 2,346.5 MB | 1,079.8 MB | 1,182,122 | Mongo internal (oplog etc). Leave alone. |

---

## `admin` database — all 70 collections

Columns:
- **Docs** — estimated count.
- **Size** — uncompressed data size on disk.
- **Idx** — number of indexes / total index size.
- **Last write** — most recent ObjectId generation time (actual document insert/replace, not the `modificationTimestamp` source field).
- **Status** — Active / Stale / Empty / View / Deprecated.

### MLS listing data (the bulk of the database)

| Collection | Docs | Size | Idx | Last write | Status |
|---|---:|---:|---:|---|---|
| `unified_closed_listings` | 1,512,601 | 9,663.6 MB | 46 / 1,306.5 MB | 2026-04-29 | **Active — canonical** |
| `unified_listings` | 81,064 | 2,740.7 MB | 62 / 201.8 MB | 2026-04-29 | **Active — canonical** |
| `unified_in_escrow` | 22,486 | 861.3 MB | 11 / 8.7 MB | 2026-04-28 | Active — canonical |
| `unified_off_market` | 32,033 | 429.6 MB | 11 / 14.2 MB | 2026-04-28 | Active — canonical |
| `unifiedclosedlistings` | 1,512,601 | 0 (view) | 0 | n/a | **View** of `unified_closed_listings` |
| `unifiedlistings` | 81,064 | 0 (view) | 0 | n/a | **View** of `unified_listings` |
| `crmls_listings` | 104,378 | 680.0 MB | 11 / 36.7 MB | 2026-04-28 | **Legacy per-MLS pipeline — duplicates unified_listings + unified_off_market for CRMLS** |
| `crmlsClosedListings` | 78,883 | 498.6 MB | 1 / 1.7 MB | 2026-04-28 | **Legacy — duplicates a subset of unified_closed_listings (CRMLS)** |
| `gpsClosedListings` | 53,077 | 361.0 MB | 1 / 1.2 MB | 2026-04-28 | **Legacy — duplicates a subset of unified_closed_listings (GPS)** |
| `closed_listings` | 28,839 | 238.0 MB | 1 / 0.7 MB | 2026-04-13 | Legacy — older closed-listings staging, mostly GPS |
| `listings` | 10,146 | 72.6 MB | 10 / 3.9 MB | 2026-04-28 | Legacy ungrouped (`mlsSource: null`); written today but no obvious consumer |
| `crmlsclosedlistings` | 0 | 0 | 10 / 0.08 MB | — | **Empty Mongoose-pluralization shadow of `crmlsClosedListings`** |
| `gpsclosedlistings` | 0 | 0 | 10 / 0.08 MB | — | **Empty Mongoose-pluralization shadow of `gpsClosedListings`** |
| `temp_flattened` | 3,198 | 21.6 MB | 2 / 0.2 MB | 2025-11-11 | **Stale — leftover scratch from a one-off flatten run** |
| `unifiedlistings_deprecated_2025_12_24` | 4,366 | 30.0 MB | 11 / 1.1 MB | 2025-12-05 | **Explicitly deprecated by name — drop after verifying no readers** |
| `photos` | 920,841 | 844.0 MB | 6 / 100.0 MB | 2026-04-28 | Active — joined by `listingId` and `listingKey` |

**By-MLS distribution in `unified_closed_listings`:** CRMLS 1,012,789 · GPS 212,288 · CLAW 127,211 · SOUTHLAND 84,924 · BRIDGE 29,217 · HIGH_DESERT 19,623 · CONEJO_SIMI_MOORPARK 19,272 · ITECH 6,627.

**By-MLS distribution in `unified_listings`:** CRMLS 49,833 · CLAW 13,601 · SOUTHLAND 6,940 · GPS 5,297 · HIGH_DESERT 3,091 · BRIDGE 1,407 · CONEJO_SIMI_MOORPARK 745 · ITECH 19.

> The `crmlsClosedListings` (78,883), `gpsClosedListings` (53,077), and `closed_listings` (28,839 GPS) data is a strict **subset** of what's already in `unified_closed_listings` for those same MLSs (verified by sampling `listingKey`s). They're costing ~1.1 GB of duplicate storage and writes.

### Geography & location

| Collection | Docs | Size | Last write | Status |
|---|---:|---:|---|---|
| `cities` | 1,047 | 0.5 MB | 2025-12-04 | Active reference data |
| `counties` | 56 | 0.0 MB | 2025-12-04 | Active reference data |
| `regions` | 3 | 0.0 MB | 2025-12-05 | Active (CA-only currently) |
| `subdivisions` | 1,426 | 4.0 MB | 2026-04-07 | **Active — CMA target** |
| `street_boundaries` | 36 | 0.0 MB | 2025-12-20 | Active (sparse coverage) |
| `location_index` | 2,658 | 0.9 MB | 2026-01-02 | Search-suggest index. Stale-ish. |
| `points_of_interest` | 3,414 | 5.1 MB | 2026-04-01 | Active (Google Places cache) |
| `californiastats` | 1 | 0.0 MB | 2025-12-07 | Single aggregate document. Looks like a one-off. |
| `neighborhoods` | 0 | 0 | — | **Empty** — Mongoose model never used, or migrated away from |

### Users, auth, and CRM

| Collection | Docs | Size | Last write | Status |
|---|---:|---:|---|---|
| `users` | 39 | 1.9 MB | 2026-04-28 | **Active — primary user collection** (NextAuth-style) |
| `payload_users` | 20 | 0.4 MB | 2025-11-24 | **Legacy / parallel** — Payload CMS user collection. Likely superseded by `users`. |
| `usersessions` | 0 | 0 | — | Empty — Mongoose model placeholder |
| `verificationtokens` | 0 | 0 | — | Empty |
| `twofactortokens` | 0 | 0 | — | Empty |
| `userpreferences` | 0 | 0 | — | Empty (preferences live on the user doc) |
| `user_goals` | 5 | 0.0 MB | 2025-11-24 | Active — AI-extracted buyer goals |
| `saved_chats` | 1 | 0.0 MB | 2025-11-22 | Barely used; AI chat history feature |
| `chat_messages` | 0 | 0 | — | Empty (paired with saved_chats?) |
| `pushsubscriptions` | 1 | 0.0 MB | 2026-04-22 | Web Push subscriptions — only 1 subscriber |
| `agentsubscriptions` | 0 | 0 | — | Empty — agent subscription tier feature unused |
| `partnerships` | 0 | 0 | — | Empty |
| `pointsledgers` | 0 | 0 | — | Empty (gamification ledger?) |
| `teams` | 2 | 0.0 MB | 2026-01-04 | Active — agent team feature |
| `contacts` | 616 | 1.1 MB | 2026-04-26 | **Active — CRM contacts (synced from FUB)** |
| `labels` | 2 | 0.0 MB | 2026-01-14 | Active — contact labels |
| `importbatches` | 24 | 0.2 MB | 2026-01-14 | Active — contact import history |
| `fub_sync_state` | 1 | 0.0 MB | n/a | Active — single doc tracking last FUB sync cursor |

### Marketing & messaging

| Collection | Docs | Size | Last write | Status |
|---|---:|---:|---|---|
| `campaigns` | 3 | 0.0 MB | 2026-04-23 | Active — outbound campaigns |
| `contactcampaigns` | 95 | 0.0 MB | 2026-04-16 | Active — campaign↔contact join |
| `campaignexecutions` | 6 | 0.0 MB | 2026-01-08 | Active — per-run metrics |
| `directmailpieces` | 243 | 0.2 MB | 2026-04-23 | Active — Thanks.io direct mail |
| `smsmessages` | 52 | 0.0 MB | 2026-03-26 | Active — Twilio SMS |
| `emailmetadatas` | 7 | 0.0 MB | 2026-01-13 | Light usage — Resend metadata wrapper |
| `voicemailscripts` | 0 | 0 | — | Empty |
| `searchactivities` | 0 | 0 | — | Empty (analytics never wired) |
| `listingviews` | 0 | 0 | — | Empty (analytics never wired) |

### Content (Payload CMS leftovers in `admin`)

| Collection | Docs | Size | Last write | Status |
|---|---:|---:|---|---|
| `articles` | 46 | 0.4 MB | 2026-04-10 | **Active — primary blog/articles content** |
| `articlerequests` | 2 | 0.0 MB | 2025-11-30 | Active — AI article generation queue |
| `generationsessions` | 0 | 0 | — | Empty |
| `blog-posts` | 0 | 0 | — | **Empty Payload artifact** — duplicate intent of `articles` |
| `media` | 0 | 0 | — | **Empty Payload artifact** |
| `schools` | 0 | 0 | — | **Empty Payload artifact** |
| `payload-kvs` | 0 | 0 | — | Empty Payload internal |
| `payload-locked-documents` | 0 | 0 | — | Empty Payload internal |
| `payload-migrations` | 0 | 0 | — | Empty Payload internal |
| `payload-preferences` | 0 | 0 | — | Empty Payload internal |
| `domain_mappings` | 0 | 0 | — | Empty (custom-domain feature stub) |
| `runwaytasks` | 19 | 0.0 MB | 2025-06-17 | **Stale** — Runway AI image-gen jobs from June 2025 |
| `openhouses` | 0 | 0 | — | **Empty** |

---

## `payload` database (orphan)

11 collections, 3 documents total, < 1 MB. This is leftover from a Payload CMS instance that was either provisioned and abandoned, or was the initial home before everything was moved into `admin`.

| Collection | Docs |
|---|---:|
| `users` | 3 |
| All others (`blog-posts`, `cities`, `contacts`, `media`, `neighborhoods`, `schools`, `payload-*`) | 0 |

**Recommendation:** the 3 user records in `payload.users` should be inspected (likely test/dev accounts) and then the entire database dropped.

## `your-db-name` database (delete)

3 empty collections (`listings`, `openhouses`, `photos`). Created by some tool that defaulted the DB name to the literal placeholder `your-db-name` — a misconfigured connection string somewhere. Zero data. Drop the database; chase down the script that wrote there if the listings/openhouses/photos names match a known integration that's been silently failing.

---

## Index audit on the largest collections

### `unified_closed_listings` — 46 indexes, 1,306 MB index size

Many of these are duplicates created by different scripts/migrations. Examples of overlap:

- **City prefix:** `city_closeDate`, `city_closeDate_closePrice`, `city_1_closeDate_1`, `city_1`, `city_status` — 5 indexes that all start with `city`. The two-field `city_closeDate` is fully covered by `city_closeDate_closePrice`.
- **Subdivision prefix:** `subdivisionName_closeDate`, `subdivisionName_closeDate_desc`, `subdivisionName_closeDate_closePrice`, `subdivisionName_1`, `subdivisionName_1_closeDate_1`, `subdivision_status` — 6 indexes.
- **propertyType prefix:** `propertyType_closeDate`, `propertyType_city_closeDate`, `propertyType_1`, `propertyType_1_closeDate_1`, `propertyType_status`, `city_propertySubType_closeDate`.
- **mlsSource:** `mlsSource_closeDate`, `mlsSource_1`, `mlsSource_1_mlsId_1`.
- **closePrice:** `closePrice_closeDate`, `closePrice_1`, `closePrice_1_closeDate_1`.
- **Two `coordinates_2dsphere` indexes** on different paths (`coordinates` and `coordinates.coordinates`) — only the GeoJSON one is needed.

Every insert/update on this collection has to maintain all 46 indexes — at 1.5M docs this is the biggest write-amplification cost we have.

### `unified_listings` — 62 indexes, 202 MB

Similar pattern, plus schema-drift duplicates from a partial field rename:
- `bedsTotal` vs `bedroomsTotal` (both indexed in 4-field composites)
- `bathsTotal` vs `bathroomsTotalInteger` (same)
- `coordinates_2dsphere` and `coordinates.coordinates_2dsphere` (same redundancy as above)

### `crmls_listings` — 11 indexes
Reasonable. But if this collection is to be retired (see cleanup doc), the indexes are moot.

### `photos` — 6 indexes
Reasonable. `listingId_1`, `listingKey_1` and the two compound forms are all justified — the question is which join key the consumer code uses.

---

## Active write pipelines (verified by ObjectId timestamps)

| Pipeline | Writes to | Driven by |
|---|---|---|
| Unified MLS sync | `unified_listings`, `unified_closed_listings`, `unified_in_escrow`, `unified_off_market` | `src/scripts/mls/backend/unified/` (Python, run on this VPS via `run-pipeline.py`) |
| Legacy per-MLS sync | `listings`, `crmls_listings`, `crmlsClosedListings`, `gpsClosedListings`, `photos` | **Unknown** — these are written today but the only cron on this VPS is FUB lead sync. Likely a Node/Next.js process running elsewhere, or `archive/` scripts that are still cron-scheduled somewhere we haven't audited. |
| FUB lead sync | `contacts`, `fub_sync_state` | `src/scripts/fub/sync-fub-leads.py` (every 15 min via this VPS's crontab) |
| CMA builder | `subdivisions` (writes `cmaStats`), reads `unified_listings`, `unified_closed_listings` | `src/scripts/cma/build-subdivision-cma.py` |
| Web app | `users`, `contacts`, `campaigns`, `articles`, etc. | Next.js application (separate deploy — not in this repo) |

> **Important to track down:** what is still writing to `crmls_listings` / `crmlsClosedListings` / `gpsClosedListings` / `listings` / `photos` today? Until that's identified, deleting these collections is unsafe. See `cleanup-and-migration.md` step 1.

---

## Schema highlights for major collections

### `users` (39 docs)
Rich user profile with: `agentProfile.{headline, headshot, heroPhoto, licenses, mlsDataSources, serviceAreas, specializations, stats, testimonials, valuePropositions, customDomain, businessHours, certifications, galleryPhotos, metaKeywords, tagline}`, `agentApplication.identityVerified`, `adAccounts.{gbp, google, meta}`, `activityMetrics.{totalSessions, totalListingsViewed, engagementScore...}`, `buyerAgreement.signed`, `dislikedListings`, `conversations`, `emailSignature`. Production-grade schema.

### `payload_users` (20 docs)
Smaller schema: `accountType`, `roles`, `isAdmin`, `password`, `swipeAnalytics`, `likedListings`, `dislikedListings`, `favoriteCommunities`, `savedSearches`, `serviceAreas`, `stripeOnboarded`, `twoFactorEnabled`, `loginAttempts`, `lastSwipeSync`. Looks like the **earlier** (Payload-CMS-driven) user collection — superseded by `users` but never cleared. Confirm no readers, migrate any unique users (`stripeOnboarded`, `swipeAnalytics`?) into `users`, then drop.

### `contacts` (616 docs)
CRM record. Fields include MLS-derived property data (`assessedValue`, `bedrooms`, `bathrooms`, `purchasePrice`, `yearBuilt`, `sqft`, `propertyType`, `address`), CRM data (`source`, `campaignHistory`, `noteHistory`, `interests.{locations, propertyTypes}`, `preferences.{emailOptIn, smsOptIn, callOptIn, preferredContactMethod}`, `consent.{tcpaConsent, marketingConsent}`, `doNotContact`, `voicemailOptOut`), and FUB linkage via `userId` + `importBatchId`.

### `unified_listings` (81K docs)
Full Spark MLS RESO field set, flattened to camelCase. Notable: `coordinates` (GeoJSON), `slug`, `slugAddress`, `mlsSource`, `mlsId`, `listingKey`, `listingId`, `lastSparkRefreshAt`, `statusLastChecked`, `lastSparkMissAt` (used by `update-status.py` to detect stale listings).

### `unified_closed_listings` (1.5M docs)
Same shape as `unified_listings` plus closed-only fields: `closeDate`, `closePrice`. The 5-year TTL index `closeDate_ttl_5years` does **not** actually have `expireAfterSeconds` set in the listing — it's just a regular ascending index. (Confirm: if a TTL was intended, it never took effect — the collection has 1.5M docs going back as far as MLS history allows.)

### `subdivisions` (1,426 docs)
Heavy nested doc with `cmaStats.{active, closed, bySubType, sampleWindow, subdivisionProfile, topActiveComps, topClosedComps, trends, quality}` — output of `build-subdivision-cma.py`. Per-subdivision CMA cache.

### `photos` (920K docs)
Lean: `listingId` (numeric MLS id), `listingKey` (long format), `photoId`, `Order` / `order` (case-mismatched legacy), `primary`, `caption`, and 8 size URIs (`uri300`, `uri640`, `uri800`, `uri1024`, `uri1280`, `uri1600`, `uri2048`, `uriThumb`, `uriLarge`).

### `articles` (46 docs)
Standard CMS article: `title`, `slug`, `content`, `excerpt`, `category`, `tags`, `featuredImage.{url, publicId, alt}`, `ogImage.{url, publicId}`, `seo.{title, description, keywords}`, `metadata.{readTime, views}`, `author.{id, name, email}`, `status`, `publishedAt`, `featured`, `year`, `month`.
