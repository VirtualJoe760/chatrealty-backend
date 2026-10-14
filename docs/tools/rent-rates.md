# Rent layers — going rate, seasonal, achieved seasonal, pool premium

Four producers write onto the same two targets: `subdivisions.rentStats` and the
per-ZIP doc in `rent_rates`. They are **additive** — none edits another's
output, so sources can be compared instead of blended.

| Block | Producer | Subs | ZIPs | What it answers |
|---|---|---:|---:|---|
| `goingRate` + base stats | `build-rent-rates.py` | 1,101 | 919 | annual unfurnished market rent |
| `seasonal` | `build-seasonal-rates.py` | 459 | 763 | peak-season **asking** rates |
| `closedSeasonal` | `build-closed-seasonal-rates.py` | 948 | 1,015 | peak-season **achieved** rates |
| `poolPremium` | `build-pool-premium.py` | 759 | 896 | monthly rent lift of a private pool |

**Refreshed:** seasonal layers Tue + Fri 1:30 AM; rent-rates and pool-premium
Tue + Fri 2 AM. **Backs:** `get_going_rate`; the other three have **no tool yet**.

## How to get it

**Access mode:** read MongoDB. All four layers live inside **one** `rentStats`
object (subdivision) or **one** `rent_rates` document (ZIP) — so you fetch them
together in a single query, never four.

**Keys:** `subdivisions.slug` (unique) · `rent_rates.postalCode` (unique,
**string** — `"92211"`, not `92211`, or the query silently returns nothing).

```ts
// subdivision — all four layers in one read
const sub = await db.collection("subdivisions").findOne(
  { slug },
  { projection: { rentStats: 1, dominantPostalCode: 1, _id: 0 } }
);

// ZIP — the whole doc IS the rent block
const zip = await db.collection("rent_rates").findOne(
  { postalCode },                     // string
  { projection: { _id: 0 } }
);
```

### Implement the fallback — it is the whole point

A subdivision layer is often `insufficient` while the ZIP has data. Fetch the
subdivision, then the ZIP named by `dominantPostalCode`, and **tell the user
which one you used**:

```ts
function pickLayer(sub, zip, layer, validBases) {
  const s = sub?.rentStats?.[layer];
  if (s && validBases.includes(s.basis)) return { ...s, source: "subdivision" };
  const z = zip?.[layer];
  if (z && validBases.includes(z.basis))
    return { ...z, source: "zip", zipUsed: zip.postalCode };
  return { source: "insufficient", basis: "insufficient" };
}

const seasonal = pickLayer(sub, zip, "seasonal", ["tiered-median"]);
const achieved = pickLayer(sub, zip, "closedSeasonal",
                           ["closed-furnished", "closed-tail-inferred"]);
const pool     = pickLayer(sub, zip, "poolPremium", ["matched-cell-median"]);
```

**Gate on `basis`, not on presence.** A block exists with
`basis: "insufficient"` and null rates when the sample was too thin — truthiness
is not enough.

`poolPremium` inverts the order: **prefer ZIP over subdivision**, because
subdivision rows include placeholder names like `"Other"` that group unrelated
properties.

### What the tool returns

Return **both** seasonal layers with their sample sizes, plus the long-term
`goingRate`. Returning one seasonal layer alone invites exactly the 2x mistake
the two-layer design exists to prevent. For a ready-made comparison, read
`cashflowProfile.rents.seasonalDivergence` instead of deriving it.

**If all layers are insufficient:** say there is not enough lease data for the
area. Never interpolate from a neighboring community.

**Cost:** two indexed `findOne`s (subdivision + its fallback ZIP).

---

## Unit discipline

> ⚠️ **The single easiest way to be wrong by 2x in this codebase.**

A seasonal **peak rate** describes a handful of peak months. It is *not* a
monthly income figure. In ZIP 92211 the achieved peak is **$10,000/mo** — for
4 months. The annualized figure is **$5,200**.

| Field | Meaning | May feed cash-flow math? |
|---|---|---|
| `seasonal.peakRate` | in-season monthly asking rate | ❌ display only |
| `seasonal.offPeakRate` | off-season monthly asking rate | ❌ display only |
| `seasonal.annualizedMonthly` | Σ(tier rate × months) ÷ 12 | ✅ |
| `closedSeasonal.seasonalRate` | in-season monthly **achieved** rate | ❌ display only |
| `closedSeasonal.longTermBaseline` | achieved annual-lease rent, same cell | ❌ context |
| `closedSeasonal.annualizedMonthlyBlended` | peak at seasonal, rest at long-term, ÷ 12 | ✅ |
| `closedSeasonal.annualizedMonthlyPeakOnly` | peak at seasonal, rest at **zero** — the floor | ✅ (conservative) |

When quoting a seasonal rate to a user, give **both** ends: *"roughly $10,000/mo
in season against a $2,800 annual baseline, which blends to about $5,200/mo
across the year."* A bare "$10,000/mo" is misleading.

---

## `goingRate` and the base rent block

Built from closed Residential-Lease listings (`propertyType: "B"` in
`unified_closed_listings`, where `closePrice` **is** the monthly rent achieved)
plus current for-rent supply.

```ts
interface RentStats {
  lastUpdated; scope: "subdivision" | "zip"; geo;
  sampleWindow: { months; startDate; endDate };
  quality: { confidence: "good"|"fair"|"insufficient"; notes: string[] };

  goingRate: { monthlyRent; annualRent; rentPerSqft;
               basis: "unfurnished-median" | ...; sampleSize };

  rented:       Dist;   // what actually leased — the authoritative signal
  active:       Dist;   // current asking supply
  unfurnished:  Dist;
  furnished:    Dist;   // { count, note } when too thin to publish
  byBedroom: Array<{ beds; bedsLabel; count; medianRent; rentPerSqft }>;
  trends: { monthly: object[]; yoyMedianRentChangePct: number | null };
  topRentedComps: Comp[];   // up to 5 — show your work

  seasonal?: SeasonalBlock;
  closedSeasonal?: ClosedSeasonalBlock;
  poolPremium?: PoolPremiumBlock;
}
// Dist = { count, medianRent, avgRent, minRent, maxRent, p25Rent, p75Rent }
```

Prefer `rented` over `active`: closed leases are achieved rent, active listings
are asking. `goingRate.basis` names which cohort produced the headline.

---

## `seasonal` — asking rates (active tiered listings)

Rolls the per-listing `rateProfile` blocks (from `build-rate-profiles.py`, which
extracts rate tiers out of `publicRemarks`) into area rates **split by bedroom**.

```ts
interface SeasonalBlock {
  basis: "tiered-median" | "insufficient";
  sampleSize: number; flatSampleSize: number;
  peakRate; offPeakRate;            // display only
  annualizedMonthly;                // ✅ the cash-flow figure
  peakMonths: number[];             // e.g. [1,2,3,4] — real, parsed from remarks
  byBed: Array<{ beds; bedsLabel; count; peakRate; offPeakRate; annualizedMonthly }>;
  quality: { confidence; notes };
}
```

**Thin by nature.** Only ~4.75% of `rateProfile` blocks parse as "tiered", so
this layer runs on roughly 750 listings valley-wide. Check `sampleSize` before
quoting — the PDCC 4-bedroom headline rested on **2 comps**, which is exactly
why the closed layer was built.

---

## `closedSeasonal` — achieved rates (closed leases)

An independent second source: leases that actually transacted, back to
2020-08-28. `unified_closed_listings` holds **254,809** closed leases — 16x the
active lease pool — of which 229,446 survive date + price sanity and 4,687 land
in the seasonal cohort (247 identified directly by the `furnished` field, 4,440
by the 2x tail rule).

```ts
interface ClosedSeasonalBlock {
  basis: "closed-furnished" | "closed-tail-inferred" | "insufficient";
  window: { months: 60; from; to };
  sampleSize;                       // seasonal-cohort closes
  directCount; inferredCount;       // furnished-field vs 2x-tail identification
  directShare;                      // fraction the furnished field confirms
  longTermSampleSize;
  seasonalRate;                     // ❌ display only — a PEAK rate
  longTermBaseline; seasonalLift;
  annualizedMonthlyBlended;         // ✅ the cash-flow figure
  annualizedMonthlyPeakOnly;        // ✅ conservative floor
  peakMonths: number[]; peakMonthsSource: "active-tiered-layer" | ...;
  windows: { m12; m24; m60 };       // drift visibility
  byBed: Array<{ beds; bedsLabel; count; directCount;
                 seasonalRate; longTermCount; longTermBaseline; seasonalLift }>;
  quality: { confidence; notes };
}
```

### Two things to communicate honestly

**1. The cohort is selected by price, so the rate sits high on purpose.** A
"seasonal" lease is one that closed at ≥ 2x its own (ZIP, beds) cell median,
with the `furnished` field as corroboration rather than the selector. Nothing
below 2x can enter, so `seasonalRate` is **not "the typical seasonal rent"** —
it is what leases priced at a clearly seasonal level achieved. `basis` tells you
which identification carried it; `closed-tail-inferred` means price alone, so
call it indicative.

**2. `byBed` has no annualized field.** It stores `seasonalRate` and
`longTermBaseline` only. The bedroom-matched blend is derived with the same
arithmetic the area level uses:

```
blended = (seasonalRate × nPeak + longTermBaseline × (12 − nPeak)) / 12
```

`build-cashflow.py` already does this — read
`cashflowStats.strategies.closedSeasonal.rentEstimate.monthlyRent` rather than
recomputing. A cell with a non-null `seasonalRate` has already cleared the
builder's minimum sample thresholds.

### `seasonalDivergence` — the comparison, precomputed

On `cashflowProfile.rents` only. Peak-to-peak, computed once so no consumer
compares an asking peak against an achieved blend:

```ts
{ askingPeakRate; askingSampleSize; achievedSeasonalRate; achievedSampleSize;
  achievedToAskingRatio;            // 1.67 in ZIP 92211
  reading: string;                  // plain-language — quote it
  note: string; }
```

`≥ 1.15` ⇒ the asking layer understates the peak. `≤ 0.85` ⇒ treat the asking
peak as aspirational. Between ⇒ the layers agree within 15%.

---

## `poolPremium` — what a private pool adds to rent

```ts
interface PoolPremiumBlock {
  basis: "matched-cell-median" | "insufficient";
  method: string; overallPctLift; sampleSize;
  byBed: object[]; quality: { confidence; notes };
}
```

**ZIP is preferred over subdivision here**, the reverse of every other layer:
subdivision rows include placeholder names like `"Other"` that group unrelated
properties together. `cashflowProfile.rents.poolPremium.source` records which
was used.

Frequently `insufficient`, and that is a real finding rather than a gap — in
country-club areas nearly every pool is association-owned, so there is no clean
private-vs-none contrast to measure. The block says this in `note`. Report it as
unavailable; do not estimate.

---

## `rateProfile` — the per-listing input

On active lease listings (`propertyType: "B"`) — **15,935 of 15,936**, i.e.
effectively complete — refreshed Mon–Sat 9:30 AM. Extracted from `publicRemarks` by a regex + Groq stack,
validated by `audit-rate-tiers.py`.

Mostly an **internal input** to `build-seasonal-rates.py`. A tool could surface
it on a lease detail page, but prefer the area-level blocks for anything
analytical — a single listing's remarks are not a market rate.

---

## Suggested MCP tools

```
get_going_rate(subdivisionSlug | postalCode, beds?)   // exists
get_seasonal_rates(subdivisionSlug | postalCode, beds?)
  → both layers side by side + the divergence reading
get_pool_premium(subdivisionSlug | postalCode)
```

For `get_seasonal_rates`, return **both** `seasonal` and `closedSeasonal` with
their sample sizes. Returning one alone invites exactly the mistake the two-layer
design exists to prevent.
