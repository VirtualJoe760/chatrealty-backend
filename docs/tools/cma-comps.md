# `cmaStats` — sale comparables

Two producers write a block named `cmaStats` to two different targets. They are
**not interchangeable**: one is an area rollup, the other is a per-listing comp
analysis with a different internal shape.

| Target | Producer | Keyed by | Refreshed | Backs |
|---|---|---|---|---|
| `subdivisions.cmaStats` | `build-subdivision-cma.py` | `slug` | daily 8 AM | `get_subdivision_cma`, `get_market_stats` |
| `unified_listings.cmaStats` | `build-listing-cma.py` | `listingKey` | Sun 1 AM | `find_comparables` |

**Coverage:** all 1,426 subdivisions · 46,768 active listings. Note `cities.cmaStats`
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

A different shape. Runs the same 5-level hierarchy / 12-factor scoring engine
the frontend used to run on demand, persisted so a comp lookup is a `findOne`
instead of a 1–20 s round-trip.

```ts
interface ListingCmaStats {
  subject: { listingKey; address; city; subdivisionName; listPrice;
             livingArea; lotSize; /* … */ };
  tier: string;                        // which fallback level produced the comps
  activeComps: Comp[];
  closedComps: Comp[];
  stats: { active: object; closed: object };
  limitations: string[];               // ← surface these
  inferences: string[];                // ← and these
  narrative: string;                   // prose summary, ready to quote
  searchCriteria: {
    levelsUsed; subdivisionMatched; dateRangeUsed;
    sqftRangeUsed; totalCandidatesEvaluated;
  };
  generatedAt: string; lastUpdated: Date;
}
```

Match hierarchy — `tier` and `searchCriteria.levelsUsed` record which fired:

1. case-insensitive exact `subdivisionName` match
2. relaxed sqft range
3. city
4. geographic radius
5. widest fallback

Reading rules:

- **`limitations` and `inferences` are the point.** They state what the comp set
  could not establish. Pass them to the user rather than presenting comps as
  definitive.
- **`narrative` is pre-written prose** — quote it instead of re-deriving a
  summary from the comp arrays.
- **Check `tier` before trusting tightness.** Level 1 comps are same-community;
  level 4–5 comps are a radius sweep and deserve a caveat.
- **Placeholder subdivision names** (`"Other"`, `"None"`, `"N/A"`,
  `"Not in a Development"`) group unrelated properties. Level-1 matches on those
  are not real community comps — the builders exclude them in area-level work
  for this reason.

## Suggested MCP tools

```
get_subdivision_cma(slug)                       // exists
find_comparables(listingKey)                    // exists
get_market_stats(city | subdivisionSlug)        // exists
```

All three already have tools. The useful improvement is **pass-through
fidelity**: ensure `limitations`, `inferences`, `quality.confidence` and
`sampleWindow` reach the model rather than being stripped to bare medians.
