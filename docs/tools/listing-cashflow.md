# `cashflowStats` — per-listing rental investment math

**Producer:** `src/scripts/cma/build-cashflow.py`
**Lives on:** `unified_listings`, every active **sale** listing (`propertyType: "A"`)
**Coverage:** ~41,862 of 47,317 priced *(2026-10-14)*
**Refreshed:** Tue + Fri 2 AM
**Backs:** `analyze_listing_cashflow`, `find_cashflowing_listings`

---

## What it is

For each active sale listing: an estimated market rent plus the full investment
math — financing, operating expenses, break-even, and two down-payment
scenarios. Written so that *"show me listings in Palm Desert that cash-flow with
20% down"* is a plain indexed Mongo filter+sort, with no math at request time.

```js
db.unified_listings.find({
  city: "Palm Desert",
  "cashflowStats.scenarios.down20.monthlyCashflow": { $gt: 0 }
}).sort({ "cashflowStats.scenarios.down20.monthlyCashflow": -1 })
```

> Needs the `city_cashflow20` / `zip_cashflow20` indexes — see
> [README.md](README.md#four-things-that-will-bite-you).

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
