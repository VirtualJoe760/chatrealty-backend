# Listing CMA — nearest like-kind comps

**Producer:** `src/scripts/comps/build-listing-cma.py`
**Lives on:** `unified_listings.cmaStats`, one per active sale listing
**Comp source:** `cma_comps` (built by `src/scripts/comps/build-cma-comps.py`)

The website reads `cmaStats` and renders it. It does **not** compute comps
itself: there is one engine, this one, so a stored CMA and a displayed CMA can
never disagree. A listing without `cmaStats` shows a "no comparables yet" state
until the next run.

## The rule: closest like-kind homes at a similar price

A comp must be all three:

| Requirement | Meaning |
|---|---|
| **Like kind** | same `propertySubType` (a condo never comps a house) and same land class — leased land only comps leased land |
| **Similar price** | within **±15%** of the subject's list price; `closePrice` for a sale, `listPrice` for an active listing |
| **Close by** | searched outward in rings of **0.5, 1, 2, 3 miles** |

The search stops at the first ring holding 5 qualifying homes; the 30 nearest
inside that ring are scored and the top 5 kept. Rings are the same at every
price. **Nothing beyond 3 miles is ever used** — a listing with no like-kind
homes inside 3 miles gets fewer comps and says so, rather than borrowing from
across the county.

Closed sales come from the last **12 months**, widening to **24** only when
fewer than 3 qualify within 3 miles. Active comps are whatever is on the market
now.

**Size gives way to distance.** There is no square-footage filter: a nearby
home that is somewhat bigger or smaller beats a same-size home across town, and
the sqft factor in the score still prefers the closer size among nearby homes.

## Why it is built this way

The engine this replaced searched subdivision, then city, then radius — radius
last. On the 40,582 CMAs it had stored, 87% took their closed comps from a
whole-city query that kept the first 20 rows the database returned with no
sort, and no scoring factor measured distance. In the Coachella Valley that
produced:

| | old engine | this engine |
|---|---:|---:|
| median distance to the farthest closed comp | 0.70 mi | 0.47 mi |
| 90th percentile | 4.85 mi | 1.59 mi |
| worst case | 513 mi | 3.0 mi |
| CMAs with any comp beyond 3 mi | 23.3% | 0% |
| closed comps outside ±15% of list price | 57.3% | 0% |

The old engine also read pool from `poolYn`/`pool`, which are empty on every
listing, so every pool home skipped its tighter search levels.

## Scoring

Thirteen factors, weights summing to 100. **Proximity is 20 at every tier**,
scoring 1.0 at the subject and falling linearly to 0 at 3 miles. The other
twelve keep their original relative weights scaled into the remaining 80:

| Factor | affordable | residential | luxury |
|---|---:|---:|---:|
| proximity | 20 | 20 | 20 |
| sqft | 20 | 16 | 13.6 |
| subdivision | 9.6 | 12 | 16 |
| bed/bath | 13.6 | 9.6 | 6.4 |
| pool/spa | 4 | 8 | 8 |
| lot size | 4 | 8 | 12 |
| view | 2.4 | 6.4 | 10.4 |
| recency | 10.4 | 8 | 4 |
| year built | 6.4 | 4 | 1.6 |
| arch style | 0 | 2.4 | 4.8 |
| stories | 4 | 2.4 | 1.6 |
| garage | 3.2 | 1.6 | 0.8 |
| land type | 2.4 | 1.6 | 0.8 |

A factor that cannot be judged (unknown pool, no subdivision) drops out and the
rest are rescaled to 100. Tier — `luxury` above $1.5M or 3,000 sqft,
`affordable` below $500K or 1,500 sqft, else `residential` — now only chooses
the weight column. It no longer sets any radius or lookback.

Pool and spa come from `poolFeatures`/`spaFeatures`, with remarks as the
fallback. A community-only pool counts as **no** private pool.

## Schema

```ts
interface CmaStats {
  engineVersion: 2;
  lastUpdated: string;                  // ISO-8601 UTC
  tier: "affordable" | "residential" | "luxury";

  subject: {
    listPrice: number; livingArea: number; lotSize: number;   // lot in sqft
    bedsTotal: number; bathsTotal: number; yearBuilt: number;
    pricePerSqft: number; propertyType: "A"; propertySubType: string;
    landType: string; pool: boolean | null; spa: boolean | null;
    view: "golf" | "mountain" | "desert" | "water" | "city" | null;
    garageSpaces: number;
  };

  // Up to 5 each, sorted by similarityScore desc.
  activeComps: Array<{
    listingKey: string; distanceMiles: number;
    address: string; city: string; subdivisionName: string | null;
    listPrice: number; livingArea: number; bedsTotal: number; bathsTotal: number;
    yearBuilt: number; listPricePerSqft: number; daysOnMarket: number;
    similarityScore: number;            // 0–100
    pool: boolean | null; spa: boolean | null; view: string | null;
    garageSpaces: number; landType: string; lotSize: number;
  }>;
  closedComps: Array<{
    listingKey: string; distanceMiles: number;
    address: string; city: string; subdivisionName: string | null;
    closePrice: number; closeDate: string;   // YYYY-MM-DD
    listPrice: number; livingArea: number; bedsTotal: number; bathsTotal: number;
    yearBuilt: number; salePricePerSqft: number;
    salePriceToListRatio: number | null; daysOnMarket: number;
    similarityScore: number;
    pool: boolean | null; spa: boolean | null; view: string | null;
    garageSpaces: number; landType: string; lotSize: number;
  }>;

  stats: {                              // over the comps above
    active: { count; avgPrice; minPrice; maxPrice; medianPrice;
              avgPricePerSqft; avgSqft; avgDaysOnMarket; avgLotSize };
    closed: { /* same */ avgSalePriceToListRatio: number | null };
  };

  searchCriteria: {
    method: "radius";
    ringMiles: [0.5, 1, 2, 3];
    ringMilesUsed: { active: number | null; closed: number | null };
    maxCompDistanceMiles: { active: number | null; closed: number | null };
    closedLookbackMonths: 12 | 24 | null;
    priceBand: 0.15;
    priceRange: [number, number] | null;   // the ±15% band in dollars
    likeKind: { propertySubType: string | null; landClass: "fee" | "lease" };
    levelsUsed: { active: number; closed: number };  // ring index, 1 = 0.5 mi … 4 = 3 mi, 0 = none
    subdivisionMatched: boolean;           // any comp shares the subject's real subdivision
    totalCandidatesEvaluated: { active: number; closed: number };
  };

  narrative: string;
  limitations: string[];                // render as caveats
  inferences: string[];                 // attributes read from remarks, not MLS fields

  quality: {
    confidence: "high" | "good" | "medium" | "low" | "insufficient";
    activeCompCount: number; closedCompCount: number;
    downgradedFor: string[];            // why confidence was lowered, if it was
  };
}
```

`ringMilesUsed` is the useful display field: "comps within 0.5 mi" is the
statement a client cares about. `levelsUsed` is kept only so readers of the old
shape do not break; its numbers no longer mean subdivision or city.

## Confidence

Start from the total comp count, then lower one notch for each of: closed comps
reaching the 3-mile ring, closed comps needing the 24-month lookback, and fewer
than 3 closed comps. A CMA never drops below `low` this way; `insufficient`
means zero comps.

| Total comps | Starting confidence |
|---:|---|
| ≥ 8 | `high` |
| 5–7 | `good` |
| 3–4 | `medium` |
| 1–2 | `low` |
| 0 | `insufficient` |

`quality.downgradedFor` lists the reasons, so the UI can say why.

## Limitations

Possible `limitations[]` entries:

- `Subject has no map coordinates — no radius search was possible.`
- `Subject has no property sub-type — comps are not restricted to like kind.`
- `Subject has no list price — comps are not restricted to a price band.`
- `Only N like-kind active listing(s) within 3 mi at ±15% of list price.`
- `Only N like-kind closed sale(s) within 3 mi at ±15% of list price.`
- `Fewer than 5 like-kind sales within X mi — closed comps reach Y mi.`
- `Closed comps required a 24-month lookback — …`
- `Pool mismatch: subject has a private pool but most closed comps do not.`

## How to read it

```ts
const listing = await db.collection("unified_listings").findOne(
  { listingKey },
  { projection: { cmaStats: 1 } }
);
const cma = listing?.cmaStats?.engineVersion === 2 ? listing.cmaStats : null;
// null -> render "no comparables yet"; never compute comps in the request.
```

Declare `cmaStats: { type: Schema.Types.Mixed }` on the Mongoose model —
`strict: true` silently drops fields PyMongo writes.

## Running it

```bash
src/scripts/comps/build-listing-cma.py --key <listingKey> --dry-run   # one, print JSON
src/scripts/comps/build-listing-cma.py --city "Palm Desert"
src/scripts/comps/build-listing-cma.py --all --workers 4
src/scripts/comps/build-listing-cma.py --all --skip-fresh-hours 0     # force everything
```

A CMA counts as fresh only if it was built by the current `engineVersion`
inside `--skip-fresh-hours` (default 60), so a version bump reseeds every
listing on the next run. Cron runs `build-cma-comps.py --all` first, because the
active comps come from its snapshot, then this, under one `flock`. At 4 workers
a full pass is about 2 hours.

## Edge cases

- **No coordinates** (about 0.6% of listings): no comps, one limitation, `insufficient`.
- **Sparse areas**: fewer than 5 comps rather than distant ones. Show the count.
- **No `propertySubType` or list price**: that filter is skipped and a limitation says so.
- **`pool: null`**: unknown. Render neither "pool" nor "no pool".
- **Active comps are a snapshot** from the last `cma_comps` build, so a comp may
  have gone pending since. `lastUpdated` says how old.

## Related

- `docs/tools/area-cma.md` — `cma_comps`, and the field traps it resolves
- `docs/tools/cma-comps.md` — both `cmaStats` blocks, for tool authors
- `docs/cma/SUBDIVISION_AND_CITY_CMA.md` — the subdivision-scope CMA
