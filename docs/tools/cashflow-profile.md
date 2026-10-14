# `cashflowProfile` — the one-findOne cash-flow answer

**Producer:** `src/scripts/cma/build-cashflow-profile.py`
**Lives on:** `subdivisions` and `cities`, keyed by `slug`
**Coverage:** 741 of 1,426 subdivisions · 836 of 1,047 cities *(2026-10-14)*
**Refreshed:** Tue + Fri 2 AM, last stage of the chain
**Backs:** an area cash-flow MCP tool — **does not exist yet**, highest-value gap

---

## What it is

The single document that answers *"what could cash-flow in Palm Desert Country
Club?"* with **no request-time aggregation and no arithmetic**. One `findOne` by
slug returns four rental strategies, per-bedroom economics, rent provenance,
confidence, and plain-language caveats in a few KB.

It **composes only**. Every economic figure is a median over the per-listing
`cashflowStats` that `build-cashflow.py` already wrote, so a rollup can never
disagree with the listing detail a user drills into.

## How to get it

**Access mode:** read MongoDB. Nothing to call — this block is written by cron.
**Key:** `slug` (unique). Resolve a user's words to a slug first — see
[how-to-call.md](how-to-call.md#3--lookup-keys-and-how-to-resolve-user-words-into-them).

```ts
// subdivision
const doc = await db.collection("subdivisions").findOne(
  { slug },                                    // e.g. "palm-desert-country-club"
  { projection: { cashflowProfile: 1, _id: 0 } }
);

// city — same block, same shape
const doc = await db.collection("cities").findOne(
  { slug },                                    // e.g. "palm-desert"
  { projection: { cashflowProfile: 1, _id: 0 } }
);
```

**What the tool returns:** `doc.cashflowProfile` **whole and unmodified**. It is
a few KB and the caveats live inside it. Do not cherry-pick fields.

**If absent** (`!doc?.cashflowProfile`): the area has no active inventory with a
rent signal. Return `{ available: false, reason }` and offer the parent city —
its slug is derivable from `geo.city`. Never substitute zeros.

**Cost:** one indexed `findOne`. No aggregation, no math, no second round-trip.

## Shape

```ts
interface CashflowProfile {
  lastUpdated: string;              // ISO — surface this when asked how current
  scope: "subdivision" | "city";
  geo: { name; slug; city; county; dominantPostalCode };

  inventory: {
    activeCount: number;            // listings behind every figure below
    medianListPrice: number; medianSqft: number; medianPricePerSqft: number;
    avgDom: number; minPrice: number; maxPrice: number;
    bedMix: BedMixEntry[];          // histogram, not an average
    poolPctPrivate: number;         // private pools only
    poolPctAny: number; poolPctCommunity: number;
    poolBreakdown: { private; community; none; unknown; withData; total };
    spaPrevalence:   { pct; withData; total };
    gatedCommunity:  { pct; withData; total };
    dominantSubType: string;
  };

  rents: {
    longTerm:       { basis; monthlyRent; sampleSize; byBed; confidence };
    seasonal:       SeasonalRents;        // ASKING  — see rent-rates.md
    closedSeasonal: ClosedSeasonalRents;  // ACHIEVED — see rent-rates.md
    seasonalDivergence: Divergence | null;
    poolPremium:    PoolPremium;
  };

  economics: {
    mortgageRatePct: number;        // live rate used for this build
    rateAsOf: string;
    assumptions: { propertyTaxRate; insuranceRate; vacancyPct; managementPct;
                   maintenancePct; closingCostPct; loanTermYears; rateSource };
    longTerm:       StrategyMedians | null;
    seasonal:       StrategyMedians | null;
    closedSeasonal: StrategyMedians | null;
    byBed: Array<{ beds; bedsLabel; count; archetypePrice; medianSqft;
                   longTerm; seasonal; closedSeasonal }>;
  };

  verdict: {
    longTerm:       Verdict;
    seasonal:       Verdict;
    closedSeasonal: Verdict;
    shortTerm:      { status: "unclear"; reason: string };  // always unclear
    notes: string[];                // user-readable caveats — REPEAT THESE
  };

  cashflowingAt20: {
    longTermCount: number; seasonalCount: number; closedSeasonalCount: number;
    topLongTerm: ListingRef[]; topSeasonal: ListingRef[];
    topClosedSeasonal: ListingRef[];     // up to 10 each
  };

  quality: { confidence: "good" | "fair" | "insufficient"; notes: string[] };
}

interface StrategyMedians {
  listingsScored: number;
  rentUsed: number;                 // median gross monthly rent assumed
  breakEvenDownPct: number;         // MEDIAN listing's break-even, 0.421 = 42.1%
  minBreakEvenDownPct: number;      // BEST listing's break-even
  neverCashflowsCount: number;      // no down payment makes these work
  monthlyCashflowAt20: number;      // negative is normal
  cashOnCashAt20Pct: number;
  dscrAt20: number;
  capRatePct: number;
  cashflowingAt20Count: number;
}

interface Verdict {
  status: "cashflows" | "negative" | "unclear";
  reason: string;                   // already written for a human — quote it
  medianMonthlyAt20?: number;       // when status === "cashflows"
  minBreakEvenDownPct?: number;     // when status === "negative"
}

interface ListingRef {
  listingKey; address; listPrice; beds; slug;
  strategy: "longTerm" | "seasonal" | "closedSeasonal";
  rentUsed: number;                 // the rent THAT strategy assumed
  breakEvenDownPct: number;
  monthlyCashflowAt20: number;
}
```

## The four strategies, and why there are four

| Strategy | Rent source | Operating assumptions |
|---|---|---|
| `longTerm` | annual unfurnished lease comps | 5% vacancy, 8% mgmt, 5% maint |
| `seasonal` | peak-season **asking** rates from tiered active listings | 15% vacancy, 20% mgmt, 8% maint |
| `closedSeasonal` | peak-season **achieved** rates from closed leases | identical to `seasonal` |
| `shortTerm` | **none exists** | n/a — permanently `unclear` |

`seasonal` and `closedSeasonal` are deliberately **not merged**. They answer
different questions and routinely disagree — in ZIP 92211 the achieved peak is
1.67x the asking peak ($10,000 vs $6,000) and both are correct. The two share
one code path and one assumption set, so they differ *only* by rent source.

**`shortTerm` is always `unclear`, never negative.** No MLS source carries
nightly rates. The `reason` tells the user what to verify (HOA minimum-lease
term, municipal STR ordinance). Do not let the model fill this in.

### Expect `closedSeasonal` to cash-flow on fewer listings than `seasonal`

This is correct behavior, not a discrepancy. The two annualized rents price
off-peak months differently, and off-peak is 8 of 12:

- `seasonal` prices every month at one of the listing's own **furnished asking**
  tiers (92211 off-peak: $3,500)
- `closedSeasonal` prices off-peak at the achieved **long-term baseline**
  (92211: $2,800 — unfurnished annual)

So the achieved layer only wins where the peak lift outruns that off-peak drag.
Nobody publishes achieved-furnished off-peak rates, so the conservative blend is
the honest one. Despite that, the achieved layer **covers 6x more inventory**
(18,835 listings scored vs 3,159) because tiered active listings are scarce and
closed leases are not.

## Reading rules for the tool and the model

1. **Return the whole block.** It is a few KB and the caveats are inside it.
   Cherry-picking a verdict and dropping `verdict.notes` is how a 2-comp number
   gets quoted as fact.
2. **Never collapse to one answer.** "Does not cash-flow" is a dead end. Give a
   per-strategy determination with its break-even, and lead with whichever
   strategies work. If none do, say how far away they are — a community where
   the best listing needs 24% down is a different conversation from one needing
   65%.
3. **Quote `verdict.reason` and `verdict.notes` verbatim.** They are already
   written for a human and they carry the provenance.
4. **`breakEvenDownPct` vs `minBreakEvenDownPct`.** The first is the median
   listing, the second the best one. They can rank cells differently — in PDCC,
   4bd has the lowest median break-even while 3bd has the best single listing.
   Say which you mean.
5. **`monthlyCashflowAt20` is often negative.** That is a real market fact in
   resort/golf communities, not an error.
6. **`rents.*.source === "zip"`** means the figure came from the fallback ZIP,
   not the community. `verdict.notes` already says so; pass it along.
7. **Honor the inventory count.** `inventory.activeCount` of 15 means every
   median is over 15 listings. Do not present it as a market-wide truth.

## Worked example — Palm Desert Country Club

15 active listings, median $529,000, live rate 7.40%:

| Strategy | Determination | Rent used | CF @20% | Median break-even | Best listing |
|---|---|---|---|---|---|
| Seasonal — achieved | does not cash-flow at 20% down | $5,400 | −$811 | **42.1%** | 24.3% |
| Seasonal — asking | does not cash-flow at 20% down | $4,704 | −$841 | 47.1% | **23.1%** |
| Long-term | does not cash-flow at 20% down | $2,712 | −$1,664 | 65.4% | 41.9% |
| Short-term | **unclear — not modelled** | — | — | — | — |

0 of 15 cash-flow at 20% down on any strategy — but seasonal cuts the required
down payment from 65% to 42–47%, and the best single listing reaches ~24%. The
block's notes also carry the critical caveat: **PDCC recorded 73 closed leases
over five years and zero at a seasonal rate**, so the seasonal case here is a
ZIP-level possibility, not a community-level fact.

`docs/cma/PDCC_CASHFLOW_SHOWCASE.md` is the full worked answer. Treat it as the
target quality bar for what this tool should be able to produce.

## Suggested MCP tool

```
get_area_cashflow(slug, scope?: "subdivision" | "city")
  → the full cashflowProfile block
```

Description should say, in as many words: *"Returns precomputed rental
cash-flow analysis for an area — four strategies, per-bedroom economics, rent
provenance and caveats. Do not calculate cash flow yourself; this is already
computed."*

Absent-case handling: no `cashflowProfile` means the area has no active
inventory with scored rent. Say that plainly and offer the parent city, whose
slug is in `geo.city`.
