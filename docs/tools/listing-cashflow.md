# `cashflowStats` — per-listing rental investment math

**Producer:** `src/scripts/investor/build-cashflow.py`
**Lives on:** `unified_listings`, every active **sale** listing (`propertyType: "A"`)
**Coverage:** 41,922 listings carry the block *(2026-10-14)*; the last full
run priced 41,862 of 47,317 candidates, the rest lacking any rent signal
**Refreshed:** Tue + Fri 2 AM
**Backs:** `analyze_listing_cashflow`, `find_cashflowing_listings`

---

## What it is

For each active sale listing: an estimated market rent plus the full investment
math — financing, operating expenses, break-even, and two down-payment
scenarios. Written so that *"show me listings in Palm Desert that cash-flow with
20% down"* is a plain indexed Mongo filter+sort, with no math at request time.

## How to get it

**Access mode:** read MongoDB. **Key:** `listingKey` (unique index
`listingKey_unique`).

### A · One listing

```ts
const doc = await db.collection("unified_listings").findOne(
  { listingKey },
  { projection: { cashflowStats: 1, listPrice: 1, unparsedAddress: 1,
                  bedsTotal: 1, livingArea: 1, city: 1, _id: 0 } }
);
```

Returns `doc.cashflowStats` whole, plus the identity fields so the answer can
name the property. **If absent:** no rent signal covers this listing's area
(~5,400 listings) — say rent cannot be estimated, never `$0`.

### B · "Which listings here cash-flow?"

A plain indexed filter + sort. **No math at request time.**

```ts
const strategyBlock = {                     // pick ONE
  longTerm:       "cashflowStats",            // long-term sits at the top level
  seasonal:       "cashflowStats.strategies.seasonal",
  closedSeasonal: "cashflowStats.strategies.closedSeasonal",
}[strategy ?? "longTerm"];
const strategyPath = `${strategyBlock}.scenarios.down20`;

const rows = await db.collection("unified_listings")
  .find(
    { city,                                 // or { postalCode }
      propertyType: "A",
      listPrice: { $gte: 25000 },           // ← junk guards, see below
      propertySubType: { $nin: ["Co-Ownership", "Own Your Own", "Timeshare"] },
      [`${strategyPath}.cashflows`]: true },
    { projection: {
        listingKey: 1, unparsedAddress: 1, listPrice: 1, bedsTotal: 1,
        livingArea: 1, slug: 1, _id: 0,
        [`${strategyPath}.monthlyCashflow`]: 1,
        [`${strategyPath}.cashOnCashPct`]: 1,
        // break-even and rent must come from the SAME strategy, not the top
        // level — the top level is long-term only. See the warning below.
        [`${strategyBlock}.breakEvenDownPct`]: 1,
        [`${strategyBlock}.rentEstimate.monthlyRent`]: 1,
        [`${strategyBlock}.rentEstimate.source`]: 1 } })
  .sort({ [`${strategyPath}.monthlyCashflow`]: -1 })
  .limit(limit ?? 10)
  .toArray();
```

**Project tightly.** A full listing document is large; returning 10 of them to
answer this question wastes most of the tool's token budget. The fields above are
enough to present a result and chain into a detail tool via `listingKey`.

### Both junk guards are mandatory

**`listPrice: { $gte: 25000 }`** — without it the sort surfaces misclassified
rentals. There is an active "sale" listing at **$795** for 800 sqft.
`build-cashflow.py` skips those when scoring, but they remain in the collection.

**`propertySubType: { $nin: [...] }`** — fractional-ownership listings price a
**share**, not the property, while the rent estimate is for the whole home. That
produces impossible yields that sort straight to the top. Measured 2026-10-14:

| subType | active | scored | showing "cash-flowing" |
|---|---:|---:|---:|
| `Co-Ownership` | 29 | 28 | **13** |
| `Own Your Own` | 16 | 14 | **5** |
| `Timeshare` | 12 | 3 | **1** |

Real example: *72985 Somera Road* is listed at **$289,000** for a 4bd/3,163 sqft
home — a co-ownership share. Rent is estimated at $5,600/mo from the 4-bedroom
ZIP median, giving a **23.3% gross yield** and making it the **#1 cash-flowing
listing in Palm Desert**. It is not an investment property at that price.

These 19 listings are only ~2% of the 869 that show as cash-flowing long-term,
but they carry the highest apparent returns, so they dominate the top of any
sort. **A ranking tool without this filter leads with them.**

> `build-cashflow.py` does not yet exclude these subtypes — it only applies the
> $25,000 floor. Until it does, the filter must live in the tool. Tracked as a
> known issue found 2026-10-14.

**Indexes required** — a filter+sort over 86k listings collscans without them:

```js
db.unified_listings.createIndex(
  { city: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "city_cashflow20" });
db.unified_listings.createIndex(
  { postalCode: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "zip_cashflow20" });
```

Equivalents are needed for the two seasonal paths if you expose those sorts.

> ### ⚠ Read every figure from the strategy you filtered on
>
> `cashflowStats.breakEvenDownPct`, `.rentEstimate` and `.scenarios` at the **top
> level are long-term only**. If you filter for listings that cash-flow
> *seasonally* and then display the top-level break-even, you are describing a
> different investment than the list you just built — the long-term figure will
> typically be far worse and the result reads as nonsense.
>
> This exact bug shipped in `build-cashflow-profile.py` and was fixed on
> 2026-10-14 (commit `6ade664`): its `top*` listing arrays quoted long-term
> economics for the seasonal lists. Take the whole set — cash flow, break-even
> and rent — from one `strategyBlock`.

> **Prefer the area rollup when the question is about an area.**
> "What could cash-flow in PDCC?" is answered better and cheaper by one
> `findOne` on `cashflowProfile` — see
> [cashflow-profile.md](cashflow-profile.md). Use this per-listing path when the
> user wants actual listings back.

## Shape

```ts
interface CashflowStats {
  lastUpdated: string;
  listPrice: number;

  // ── long-term result stays at the TOP LEVEL, unchanged ──
  // Existing MCP queries read cashflowStats.scenarios.down20. Do not move these.
  rentEstimate: RentEstimate;
  capRatePct: number; noiAnnual: number; grossYieldPct: number;
  fixedCosts: FixedCosts;
  scenarios: { down20: Scenario; down25: Scenario };
  breakEvenDownPct: number | null;   // null ⇒ no down payment makes it work
  breakEvenNote: string | null;

  strategies: {
    longTerm:       Strategy;          // same figures as top level
    seasonal:       Strategy | null;   // ASKING seasonal rates
    closedSeasonal: Strategy | null;   // ACHIEVED seasonal rates
    shortTerm: { available: false; reason: string };
  };

  assumptions: {
    mortgageRate: number;              // 0.074 = 7.4%, live from Freddie Mac PMMS
    loanTermYears; propertyTaxRate; insuranceRate;
    vacancyPct; managementPct; maintenancePct; closingCostPct;
    downScenarios: number[];           // [0.20, 0.25]
    rateSource: "live" | "cached" | "default";
    note: string;
  };
}

interface Scenario {
  downPct; downPayment; loanAmount; monthlyPI;
  monthlyCashflow; annualCashflow; cashOnCashPct; dscr;
  cashflows: boolean;                  // the field to filter on
}

interface FixedCosts {
  grossRent; vacancy; propertyTax; insurance; hoa; management; maintenance;
  operatingExpensesMonthly;            // includes vacancy
}

interface RentEstimate {
  monthlyRent: number;
  source: "subdivision-bedroom" | "subdivision-going-rate"
        | "zip-bedroom" | "zip-going-rate" | "zip-rent-per-sqft";
  confidence: "high" | "medium" | "low";
  geo: object;
}

interface Strategy {
  fixedCosts; scenarios; capRatePct; noiAnnual; grossYieldPct;
  breakEvenDownPct; breakEvenNote;
  rentEstimate: RentEstimate;          // the rent THIS strategy used
  assumptionOverrides?: object;        // seasonal layers only
  note?: string;
}
```

## Rent estimation fallback

Recorded in `rentEstimate.source` — read it and tell the user which fired:

1. `subdivision-bedroom` — community rentStats, bedroom-matched *(high)*
2. `subdivision-going-rate` — community going rate *(medium)*
3. `zip-bedroom` — ZIP rent_rates, bedroom-matched *(medium)*
4. `zip-going-rate` — ZIP going rate *(medium)*
5. `zip-rent-per-sqft` — rent/sqft × subject sqft *(low)*

No rent signal at all ⇒ **no `cashflowStats` is written**. Absence means
"unknown", never "zero". ~5,424 listings fall here.

## The one safe request-time calculation

`fixedCosts` is stored precisely so a tool can re-derive cash flow for a
**user-supplied** down payment or rate without a rebuild. The rent estimate is
the expensive part and it is precomputed; the financing is trivial arithmetic:

```
monthlyCashflow = (grossRent − vacancy)
                − (propertyTax + insurance + hoa + management + maintenance)
                − monthlyPI(price × (1 − down), rate, term)
```

This is the **only** sanctioned request-time math in the stack. Use it for
"what if I put 30% down?" — do not use it to invent a rent.

## Seasonal strategies

Both seasonal layers use vacation-rental operating assumptions (20% management,
15% vacancy, 8% maintenance) against the long-term 8/5/5. They differ **only**
in rent source:

- `strategies.seasonal` — from `annualizedMonthly` (asking tiers)
- `strategies.closedSeasonal` — from `annualizedMonthlyBlended` (achieved)

Either can be `null` when no seasonal signal covers the listing's area.
Measured 2026-10-14: `seasonal` on 3,159 listings, `closedSeasonal` on 18,835.

**Never read `seasonalRate` or `peakRate` as a monthly income figure.** See
[rent-rates.md](rent-rates.md#unit-discipline).

## Reading rules

1. **Filter on `scenarios.down20.cashflows`**, not on a recomputed number.
2. **`breakEvenDownPct: null` means never** — operating costs exceed effective
   rent at any down payment. Distinct from a high-but-finite break-even.
3. **A listing can cash-flow seasonally and not long-term**, and vice versa.
   Filter on the strategy the user asked about, and say which one you used.
4. **`assumptions` are the audit trail.** When a user disputes a number, the
   rate, tax rate and expense ratios that produced it are right there.
5. **Property tax is derived from list price** (1.25% CA effective) — there is
   no `taxAnnualAmount` in the feed, and Mello-Roos / special assessments are
   not modelled. Say so for new-construction and CFD-heavy areas.
6. **Listings under $25,000 are skipped** — that price is junk data or a
   misclassified rental, and it would otherwise top every cash-flow sort.

## Suggested MCP tool shape

```
analyze_listing_cashflow(listingKey, downPct?, rate?)
  → cashflowStats, plus a re-derived scenario if downPct/rate supplied

find_cashflowing_listings(city | postalCode, strategy?, minCashflow?, limit?)
  → indexed find+sort on the chosen strategy
```

For `find_cashflowing_listings`, default `strategy` to `longTerm` but expose
`seasonal` and `closedSeasonal` — in resort markets the seasonal strategies are
where the positive results are, and 2,108 listings cash-flow on achieved
seasonal rates against 869 on long-term.
