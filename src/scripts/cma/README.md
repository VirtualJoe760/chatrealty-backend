# CMA Builder Scripts

**Location**: `/root/jpsrealtor/src/scripts/cma/`
**Cron**: `0 8 * * * build-subdivision-cma.py --all` (sale CMA); `0 2 * * 2,5 build-rent-rates.py && build-cashflow.py` (rental)
**Last Updated**: 2026-06-09

Operational doc for the CMA generator. For the full data-model architecture and rationale, see [`docs/cma/SUBDIVISION_AND_CITY_CMA.md`](/docs/cma/SUBDIVISION_AND_CITY_CMA.md).

---

## What lives here

```
cma/
├── build-subdivision-cma.py     ← Daily 8 AM: cmaStats (sale comps) on every subdivision
├── build-listing-cma.py         ← Weekly: cmaStats on every active sale listing
├── build-rate-profiles.py       ← Mon–Sat: per-listing seasonal rateProfile (lease remarks)
├── build-rent-rates.py          ← Tue+Fri 2 AM: rentStats "going rate" per subdivision + ZIP
└── build-cashflow.py            ← Tue+Fri (after rent-rates): cashflowStats per sale listing
```

The CMA scripts cover the **sale** side; `build-rent-rates.py` + `build-cashflow.py` are the **rental / investment** side. See [`docs/cma/RENTAL_CASHFLOW.md`](/docs/cma/RENTAL_CASHFLOW.md) for the rental architecture + the MCP/tool-executor contract.

---

## Rental stack — `build-rent-rates.py` & `build-cashflow.py`

Two layers that power the "show me listings in X that cash-flow with 20% down" chat tool.

**`build-rent-rates.py`** — reads closed (rented) Residential-Lease listings (`closePrice` = the rent it leased at) and computes a `rentStats` "going rate" block, written to **both** `subdivisions.rentStats` and a ZIP-keyed **`rent_rates`** collection. ZIP coverage (~99% of listings carry a postalCode) closes the ~43% subdivision-orphan gap. Handles the desert seasonal-rent trap by segmenting furnished/unfurnished and using a trimmed-median fallback (there's no frequency/term field in the data).

```bash
python3 build-rent-rates.py --subdivision "La Quinta Cove" --dry-run
python3 build-rent-rates.py --zip 92253 --dry-run
python3 build-rent-rates.py --city "Palm Desert"        # subs + ZIPs in the city
python3 build-rent-rates.py --all                       # full run (cron)
python3 build-rent-rates.py --all --subs-only           # skip ZIPs
python3 build-rent-rates.py --all --zips-only           # skip subdivisions
```

**`build-cashflow.py`** — for each active sale listing, estimates rent from the layer-1 going rate (subdivision-bedroom > zip-bedroom > going-rate > rent-per-sqft), runs the financing + expense math for 20%/25%-down scenarios, and writes a `cashflowStats` block. Stores `fixedCosts` + `assumptions` so the tool executor can re-derive any down-payment / rate at query time without a rebuild. **Reads layer-1 output, so it must run after `build-rent-rates.py`** (the cron chains them).

```bash
python3 build-cashflow.py --city "Palm Desert" --dry-run
python3 build-cashflow.py --all
python3 build-cashflow.py --all --rate 0.0675 --skip-fresh-hours 60
```

Financing defaults (all CLI-overridable, echoed into `cashflowStats.assumptions`): 7.0%/30-yr loan, 1.25% tax (derived from price — no `taxAnnualAmount` in MLS; Mello-Roos not modeled), 0.4% insurance, 5% vacancy, 8% management, 5% maintenance, 3% closing.

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

Current match rule is exact-case-insensitive on `name`. In practice, ~43% of listing `subdivisionName` values don't exactly match any `subdivisions.name` (examples: `"Indian Ridge"` vs doc `"Indian Ridge Country Club"`, or dirty MLS values like `"48 @ Arenas"`). Those listings don't contribute to any subdivision's CMA.

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
- Upstream dependencies: `update-status.py` keeps the on-market collections honest; `historical_closed_sync.py` keeps `unified_closed_listings` fresh
