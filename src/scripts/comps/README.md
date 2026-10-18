# Comps scripts

What things sell for.

```
comps/
├── build-subdivision-cma.py   daily 8 AM: cmaStats on every subdivision
└── build-listing-cma.py       Sundays: cmaStats on every active sale listing
```

Both write a field named `cmaStats`, on different collections — the subdivision
one is an area aggregate, the listing one is that listing's own comp set.
Architecture is in `docs/cma/SUBDIVISION_AND_CITY_CMA.md` and
`docs/cma/LISTING_CMA.md`.

---

## `build-subdivision-cma.py`

Walks every doc in `subdivisions`, pulls its listings from `unified_listings` (active) and `unified_closed_listings` (closed), computes a `cmaStats` subdocument, and writes it back.

### Scope

- **Iterates existing subdivision docs only.** Does not auto-seed new subdivisions, does not strip legacy fields (`listingCount`, `avgPrice`, `medianPrice`), does not canonicalize aliases.
- **Match rule**: case-insensitive exact match on `subdivisionName` == `subdivisions.name`, scoped to the same `city`.
- **Filters**: `propertyType == 'A'` (Residential). Excludes `Residential Lease` (B) and anything with `landType` containing "lease". Sales only.
- **Active statuses**: `Active` + `ComingSoon` only. `Pending` / `AUC` live in `unified_in_escrow` and are not used for CMA per product decision; `Hold` / `Expired` etc. live in `unified_off_market` and are also excluded.

### Sampling

- Up to **25 most recent closings** (sorted by `closeDate` desc).
- **All current actives** in the subdivision.
- **12-month window** for the `trends`, `absorptionRate`, and quality calculations.
- **Top 5** listingKeys for both active and closed comps.

### Output shape — `cmaStats` on the subdivision doc

```js
{
  lastUpdated: "2026-04-22T08:54:00Z",

  active: {
    count,
    medianPrice, avgPrice, minPrice, maxPrice,
    medianPricePerSqft, avgDom, medianSqft,
    // extras
    avgSqft, avgPricePerSqft, avgBeds, avgBaths,
  },

  closed: {
    count,
    medianClosePrice, avgClosePrice,
    medianPricePerSqft, avgDom, saleToListRatio,
    // extras
    minClosePrice, maxClosePrice, avgPricePerSqft,
    avgPriceReductionPct, sampleStartDate, sampleEndDate,
  },

  absorptionRate,                  // closedPerMonth / activeCount (0.25 = 25% monthly absorption)

  topActiveComps:  [listingKey,…], // up to 5
  topClosedComps:  [listingKey,…], // up to 5

  subdivisionProfile: {
    poolPrevalence,                // fraction 0.0–1.0 (or null if poolYn not populated)
    spaPrevalence,
    garagePrevalence,              // fraction of listings with garageSpaces > 0
    gatedCommunity,                // bool: prevalence > 50% (or null)
    seniorCommunity,               // bool: prevalence > 50% (or null)
    // extras
    dominantSubType,               // mode: "Single Family Residence", "Condominium", etc.
    typicalGarage, typicalBeds, typicalBaths,
    typicalSqftRange: { p25, median, p75 },
    typicalYearBuiltRange: { p25, median, p75 },
  },

  quality: {
    confidence: "good" | "fair" | "insufficient",
    notes: [...]
  },
  sampleWindow: { months: 12, startDate, endDate, listingCap: 25 },
  bySubType: [ { subType, activeCount, closedCount, medianSalePrice, ... } ],
  trends: {
    monthly: [ { month: "2026-03", closedCount, medianSalePrice, medianSalePpsf, avgDom } ],
    yoyMedianPriceChangePct,
    yoyMedianPpsfChangePct,
  }
}
```

Spec-required fields (`lastUpdated`, `active.*`, `closed.*`, `absorptionRate`, `topActiveComps`, `topClosedComps`, `subdivisionProfile.*`) are always present. Extras (`quality`, `sampleWindow`, `bySubType`, `trends`) are additive — safe for the frontend to ignore.

### Skip / insufficient rules

- **0 listings** (no active + no closed): skip the subdivision entirely. No write.
- **Low signal** (<3 closed in the 12-month window AND <3 actives): write a minimal record with `quality.confidence = "insufficient"` and null stat fields. Lets the frontend render "limited data" rather than "not computed yet."
- **Normal case**: full `cmaStats` block, `confidence` = `"good"` or `"fair"`.

### CLI

```bash
# Nightly run (cron does this)
python3 build-subdivision-cma.py --all

# One subdivision by display name (case-insensitive)
python3 build-subdivision-cma.py --subdivision "La Quinta Cove"

# By slug (alias)
python3 build-subdivision-cma.py --slug la-quinta-cove

# All subdivisions in a city
python3 build-subdivision-cma.py --city "Palm Desert"

# Dry-run — compute, print first stats block in full, no DB writes
python3 build-subdivision-cma.py --all --dry-run

# Verbose per-subdivision logging
python3 build-subdivision-cma.py --all --verbose

# Cap for testing
python3 build-subdivision-cma.py --all --limit 10 --dry-run
```

### Expected runtime

~55 minutes for 1,426 subdivisions. Most of the time is the Mongo aggregation queries against `unified_closed_listings` (~1.5M docs). Each batch of 200 subdivisions writes in ~3–4 seconds.

### Log format

Matches the spec:
```
Processing 1/1426: La Quinta / La Quinta Cove               53 active,  25 closed, median $619,000
```

### Known limitation — subdivisionName orphans

Current match rule is exact-case-insensitive on `name`. In practice 29.1% of listing `subdivisionName` values don't match any `subdivisions.name` (examples: `"Indian Ridge"` vs doc `"Indian Ridge Country Club"`, or dirty MLS values like `"48 @ Arenas"`). Those listings don't contribute to any subdivision's CMA.

The fix is alias-based matching — `subdivisions` docs get an `aliases: string[]` field populated by a separate audit pipeline (`audit-subdivision-coverage.py` + `apply-shadow-merges.py`). The builder's match clause then becomes:

```python
{"$or": [
    {"subdivisionName": {"$regex": f"^{name}$", "$options": "i"}},
    {"subdivisionName": {"$in": sub.get("aliases", [])}},
]}
```

Those audit scripts existed on the old server and are known to have resolved the orphan issue; they haven't been rebuilt here yet. Memory note: `jpsrealtor subdivision-orphan coverage gap was fixed`.

### Where the output shape came from

Aligned to the frontend Claude's spec handed over on 2026-04-22. Any future frontend changes that rename `cmaStats` fields need matching changes here (and vice versa).

### Related

- Reads: `subdivisions`, `unified_listings`, `unified_closed_listings`
- Writes: `subdivisions.cmaStats` only (never touches identity fields or legacy `listingCount` / `avgPrice`)
- Upstream dependencies: `update-status.py` keeps the on-market collections honest; `historical/sync.py` keeps `unified_closed_listings` fresh
