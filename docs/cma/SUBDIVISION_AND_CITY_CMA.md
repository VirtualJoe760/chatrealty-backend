# Subdivision & City CMA — Architecture

**Location**: `/root/jpsrealtor/docs/cma/SUBDIVISION_AND_CITY_CMA.md`
**Last Updated**: 2026-04-22
**Status**: Subdivision CMA is live; City CMA is planned (roll-up from subdivisions)

---

## Overview

The CMA (Comparative Market Analysis) system pre-computes market statistics for every subdivision (and, later, every city) and stores them in MongoDB so the frontend route is a pure `findOne` lookup — **no request-time math**.

Two scopes:

1. **Listing-level CMA** (already exists in the frontend): generates an on-the-fly report for one subject property. Uses a tiered comp-search hierarchy and remarks parser. Lives under `src/lib/analytics/` on the frontend. Not managed by this backend pipeline.
2. **Subdivision CMA** (this document): pre-computed nightly. Describes the market for a whole community as a unit. Persisted onto each subdivision doc. **This is what the cron at 08:00 builds.**
3. **City CMA** (planned): roll-up aggregate of all subdivision CMAs within a city, plus a `"non-subdivision"` bucket for listings that have no subdivision match. Will be its own script (`build-city-cma.py`) keyed off data produced by #2.

---

## Why subdivisions-first, not listings-first or city-first

A subdivision is naturally a homogeneous comp cohort — same builder, similar floor plans, same HOA and amenities, same school zone. Aggregates built from a subdivision's own sales are far more defensible than a city-wide blob. And once you have subdivisions, the city number is a straightforward weighted roll-up.

Subdivision CMAs are also valuable as standalone reports (on the subdivision detail page), not just as city-CMA inputs. So the hierarchy gets reuse for free.

---

## Data sources

| Collection | Role | Notes |
|---|---|---|
| `subdivisions` | Iteration target + write target | ~1,426 docs. CMA builder writes `cmaStats` subdocument; never touches identity fields. |
| `unified_listings` | Active comps | `standardStatus in ['Active','ComingSoon']`, `propertyType='A'` (Residential), lease-excluded. |
| `unified_closed_listings` | Closed comps | Sorted by `closeDate` desc, top 25 most recent; lease-excluded at read time. |

Explicitly **not** used by the subdivision CMA:
- `unified_in_escrow` — product decision: pending deals don't represent the market, they represent in-progress commitments. May be added later as a "market momentum" indicator.
- `unified_off_market` — didn't sell. Including them would pollute closed-sale aggregates.

---

## The `cmaStats` subdocument

Written to each subdivision as `{ $set: { cmaStats: {...} } }`. See [`src/scripts/cma/README.md`](../../src/scripts/cma/README.md) for the full field list; here's the shape reference:

```ts
cmaStats: {
  lastUpdated: ISOString,

  active: {
    count: number,
    medianPrice: number,
    avgPrice: number,
    minPrice: number,
    maxPrice: number,
    medianPricePerSqft: number,
    avgDom: number,
    medianSqft: number,
    // + extras
  },

  closed: {
    count: number,
    medianClosePrice: number,
    avgClosePrice: number,
    medianPricePerSqft: number,
    avgDom: number,
    saleToListRatio: number,   // closePrice / originalListPrice
    // + extras
  },

  absorptionRate: number | null,   // (closed_last_12mo / 12) / active_count

  topActiveComps: string[],        // up to 5 listingKeys
  topClosedComps: string[],        // up to 5 listingKeys

  subdivisionProfile: {
    poolPrevalence: number | null,       // fraction, 0.0–1.0
    spaPrevalence: number | null,
    garagePrevalence: number | null,
    gatedCommunity: boolean | null,      // prevalence > 50%
    seniorCommunity: boolean | null,
    // + extras
  },

  // Extras (additive — frontend can ignore)
  quality: { confidence: "good"|"fair"|"insufficient", notes: string[] },
  sampleWindow: { months: 12, startDate, endDate, listingCap: 25 },
  bySubType: [{ subType, activeCount, closedCount, ... }],
  trends: { monthly: [...], yoyMedianPriceChangePct, yoyMedianPpsfChangePct },
}
```

---

## Design decisions

### Scope is "existing subdivisions only"

The builder does **not** auto-seed new subdivision docs. If listings reference a `subdivisionName` that has no matching `subdivisions.name` doc, those listings simply don't contribute to any CMA. This is a known limitation (see [Orphan rate](#orphan-rate-limitation)) and is addressed by a separate alias-mapping pipeline that isn't rebuilt in the current codebase.

Rationale: subdivision docs carry manually-curated content (descriptions, photos, community features) that shouldn't be auto-created from a stray typo in a listing. Auto-seeding belongs in `audit-subdivision-coverage.py` + human review.

### Sample window: 12 months, capped at 25 most recent closings

- **12 months** reflects the "current market" — older sales get weighted down by price appreciation anyway.
- **Cap at 25** keeps the summary statistics stable for active communities while still capturing enough signal.
- **No minimum floor on closings** — subdivisions with fewer than 3 closings in the window get `quality.confidence: "insufficient"` but a record is still written so the frontend can distinguish "limited data" from "not computed yet."

### Top comps: by completeness, then recency (closed) or price (active)

The spec asked for "top 5 best comparable." For a subdivision (no subject property), "best" can't mean proximity. The builder uses:

- **Top 5 closed comps**: ranked by (field-completeness score, then close-date recency). More-complete and more-recent sales go first.
- **Top 5 active comps**: ranked by (field-completeness score, then listing price descending). Showcases the higher-value inventory for that community.

Both arrays are **listingKey strings only**; the frontend fetches the live listing docs via those keys (keeps the subdivision doc small).

### Majority-wins profile

A subdivision's character is captured by the **prevalence** of each amenity across its entire listing population (active + closed sample). The builder emits:

- **Raw prevalences** (`poolPrevalence`, `spaPrevalence`, `garagePrevalence`) as fractions `0.0–1.0` or `null` if the underlying field is unpopulated in the source listings.
- **Boolean majorities** (`gatedCommunity`, `seniorCommunity`) as `true` if prevalence exceeds 50%.

**Why raw numbers for pool/spa/garage but booleans for gated/senior**: pool/spa/garage listings-level presence is often reasoning fuel for the frontend (e.g., "87% of this subdivision has pools — this home's lack of a pool is unusual"). Gated/senior are either-or categorical facts about the place.

### Insufficient data — current handling

- **0 listings, active and closed** → skip entirely (no write).
- **<3 closed in window AND <3 actives** → write minimal record with:
  ```js
  { lastUpdated, active: {count}, closed: {count}, absorptionRate: null,
    topActiveComps: [], topClosedComps: [],
    subdivisionProfile: { all_null },
    quality: { confidence: "insufficient", notes: [...] } }
  ```
  Lets the frontend render "limited data" messaging.
- **Otherwise** → full `cmaStats`.

---

## Pipeline timing

```
06:00  unified MLS incremental  → refreshes listings modified in last 25h
07:00  update-status            → moves status transitions to correct collection
                                   (Active→Pending moves to unified_in_escrow;
                                    Pending→Closed moves to unified_closed_listings)
08:00  build-subdivision-cma    → reads refreshed data, rebuilds cmaStats on every sub
10:00  historical_closed_sync   → rolling 30-day window of closings, topping up the
                                   closed collection (backup for update-status)
```

The 1-hour gap between the 6am incremental + 7am status update and the 8am CMA build is enough padding on a busy morning.

---

## Frontend integration

The subdivision CMA route becomes a pure `findOne`:

```ts
const sub = await Subdivision.findOne({ slug }, { cmaStats: 1, name: 1, city: 1 });
return Response.json(sub.cmaStats);
```

Route: `GET /api/cma/subdivision/[slug]` (frontend responsibility).

---

## City CMA (planned, not yet built)

Design for `build-city-cma.py` when we get to it:

1. Iterate `cities` collection.
2. For each city, aggregate across all `subdivisions.cmaStats` in that city (weighted by each sub's total count).
3. Plus a `"non-subdivision"` bucket computed from `unified_listings` + `unified_closed_listings` where `city == X` and `subdivisionName` doesn't match any known subdivision or its aliases. This catches the ~43% orphan listings that never contribute to a subdivision CMA.
4. Write `cmaStats` onto the city doc, same shape as subdivisions but without the `topComps` narrowing.

Runs at **08:30** (after subdivisions at 08:00, before photo caching at 09:00).

---

## Orphan-rate limitation

Historically ~43% of `subdivisionName` values in Palm Springs listings don't exactly match any `subdivisions.name`. Examples:

- `"Indian Ridge"` in listings vs `"Indian Ridge Country Club"` as the subdivision doc (name variant)
- `"48 @ Arenas"` with no matching doc (genuinely unseeded community)
- `"Alpine Village/Pyn P"` — dirty slash/abbreviation in the listing data

Those listings don't contribute to any subdivision's `cmaStats` under the current exact-match rule. Consequence: known high-value communities (Indian Ridge CC, Desert Willow, Palm Desert Greens CC, The Sommerset, Outdoor Resorts PS, Silver Spur Mobile Manor, etc.) came back `confidence: "insufficient"` when they shouldn't have.

### Fix (two-phase, lives in a future audit pipeline)

**Phase A — Diagnostic (`audit-subdivision-coverage.py`, read-only)**
- Per city, three-way diff of listing names vs subdivision doc names
- Fuzzy-match orphan docs against unmatched listing names
- Output two CSVs for human review:
  - `proposed_aliases.csv` — orphan doc + suggested aliases + listing counts
  - `proposed_new_subdivisions.csv` — unmatched listing names with ≥3 listings

**Phase B — Apply approved aliases (`apply-shadow-merges.py`)**
- Adds `aliases: string[]` field to approved subdivision docs
- Updates `build-subdivision-cma.py`'s match clause to:
  ```python
  {"$or": [
      {"subdivisionName": {"$regex": f"^{name}$", "$options": "i"}},
      {"subdivisionName": {"$in": sub.get("aliases", [])}},
  ]}
  ```

Neither script is currently in the tree. **Memory note**: these existed on the old server and demonstrably resolved the orphan issue. When reconnecting to the real repo, pull them in.

---

## Related

- **Code**: [`src/scripts/cma/build-subdivision-cma.py`](../../src/scripts/cma/build-subdivision-cma.py)
- **Upstream pipeline docs**: [`src/scripts/mls/backend/unified/README.md`](../../src/scripts/mls/backend/unified/README.md)
- **Closed-listings docs**: [`src/scripts/mls/backend/unified/closed/README.md`](../../src/scripts/mls/backend/unified/closed/README.md)
- **Cron schedule**: `/root/crontab.vps`
- **CMA output log**: `/var/log/cma/build-subdivision-cma.log`
