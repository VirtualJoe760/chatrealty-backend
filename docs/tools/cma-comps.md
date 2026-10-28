# `cmaStats` — sale comparables

Two producers write a block named `cmaStats` to two different targets. They are
**not interchangeable**: one is an area rollup, the other is a per-listing comp
analysis with a different internal shape.

| Target | Producer | Keyed by | Refreshed | Backs |
|---|---|---|---|---|
| `subdivisions.cmaStats` | `build-subdivision-cma.py` | `slug` | daily 8 AM | `get_subdivision_cma`, `get_market_stats` |
| `unified_listings.cmaStats` | `build-listing-cma.py` | `listingKey` | Sun 1 AM | `find_comparables` |

**Coverage:** all 1,426 subdivisions · every active sale listing that has coordinates. Note `cities.cmaStats`
is **not** populated (0 docs) — city-level sale stats are not built; use
`inventoryProfile` and `cashflowProfile` for cities.

---

## How to get it

**Access mode:** read MongoDB. Two different blocks, two different keys — and
**different internal shapes** despite the shared field name.

```ts
// A · area rollup — key: subdivisions.slug (unique)
const area = await db.collection("subdivisions").findOne(
  { slug },
  { projection: { cmaStats: 1, name: 1, city: 1, _id: 0 } }
);

// B · per-listing comps — key: unified_listings.listingKey (unique)
const listing = await db.collection("unified_listings").findOne(
  { listingKey },
  { projection: { cmaStats: 1, unparsedAddress: 1, listPrice: 1, _id: 0 } }
);
```

**What to return.** For B, prefer `cmaStats.narrative` (pre-written prose) plus
`limitations`, `inferences` and `tier` over dumping the full `activeComps` /
`closedComps` arrays — those are large and the narrative already summarizes
them. Return the arrays only when the user asks to see the comps.

**Do not use `cities.cmaStats` — it does not exist** (0 documents). For
city-level work use `inventoryProfile` and `cashflowProfile`.

**If absent:** the listing or subdivision has no comp set. Say so; do not widen
the search yourself — the builder already tried five fallback levels and
recorded which one it reached in `tier`.

**Cost:** one indexed `findOne` each.

---

## `subdivisions.cmaStats` — area rollup

Reads sale listings (Residential only, leases excluded) from both
`unified_listings` and `unified_closed_listings`.

```ts
interface SubdivisionCmaStats {
  lastUpdated: string;
  active: {
    count; medianPrice; avgPrice; minPrice; maxPrice;
    medianPricePerSqft; avgPricePerSqft; avgDom;
    medianSqft; avgSqft; avgBeds; avgBaths;
    bedMix: object[];
  };
  closed: {
    count; medianClosePrice; avgClosePrice; medianPricePerSqft;
    avgDom; saleToListRatio; minClosePrice; /* … */
  };
  absorptionRate: number;              // months of inventory
  topActiveComps: Comp[];              // up to 5
  topClosedComps: Comp[];              // up to 5
  subdivisionProfile: {
    poolPrevalence; spaPrevalence; garagePrevalence;
    gatedCommunity; seniorCommunity; dominantSubType; typicalGarage;
  };
  sampleWindow: { months; startDate; endDate; listingCap };
  trends: { monthly; yoyMedianPriceChangePct; yoyMedianPpsfChangePct };
  quality: { confidence; notes };
}
```

Reading rules:

- **`closed` is the authoritative price signal**; `active` is asking.
  `saleToListRatio` is the bridge between them.
- **`avgDom` is a neutral metric.** A long time on market has many legitimate
  explanations. Report it as a fact; never characterize a listing as stale,
  overpriced or distressed.
- **`absorptionRate`** is months of inventory — pair it with `count` so the user
  can see the sample behind it.
- **`sampleWindow.listingCap`** means the sample was capped; large subdivisions
  are not exhaustive.
- Prefer `inventoryProfile.bedMix` over `cmaStats.active.bedMix` when you also
  need pool/gated prevalence with explicit `withData` denominators.

## `unified_listings.cmaStats` — per-listing comps

A different shape: the nearest like-kind comps for one listing. A comp must
share the subject's `propertySubType` and land class, sit within ±15% of its
list price, and be found by searching outward in 0.5/1/2/3-mile rings — never
beyond 3 miles. Full rule, schema and confidence rubric:
[`docs/cma/LISTING_CMA.md`](../cma/LISTING_CMA.md). The website reads it and does
not compute comps itself.

```ts
interface ListingCmaStats {
  engineVersion: 2;                    // only trust 2; older shapes are the retired engine
  tier: string;                        // selects scoring weights only
  subject: { listPrice; livingArea; lotSize; propertySubType; pool; /* … */ };
  activeComps: Comp[];                 // each carries distanceMiles
  closedComps: Comp[];
  stats: { active: object; closed: object };
  searchCriteria: { ringMilesUsed; maxCompDistanceMiles; closedLookbackMonths;
                    priceRange; likeKind; /* … */ };
  limitations: string[];               // ← surface these
  inferences: string[];                // ← and these
  narrative: string;                   // prose summary, ready to quote
  quality: { confidence; downgradedFor: string[] };
  lastUpdated: string;
}
```

Reading rules:

- **`limitations` and `inferences` are the point.** They state what the comp set
  could not establish. Pass them to the user rather than presenting comps as
  definitive.
- **`narrative` is pre-written prose** — quote it instead of re-deriving a
  summary from the comp arrays.
- **Quote distance, not tier.** `searchCriteria.ringMilesUsed` ("comps within
  0.5 mi") is what a client cares about; `tier` only picks scoring weights.
- **Fewer comps is honest, not broken.** A sparse area returns fewer than five
  rather than reaching past 3 miles — say how many there are.

## Suggested MCP tools

```
get_subdivision_cma(slug)                       // exists
find_comparables(listingKey)                    // exists
get_market_stats(city | subdivisionSlug)        // exists
```

All three already have tools. The useful improvement is **pass-through
fidelity**: ensure `limitations`, `inferences`, `quality.confidence` and
`sampleWindow` reach the model rather than being stripped to bare medians.
