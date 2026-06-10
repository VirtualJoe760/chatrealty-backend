# Rental Going-Rate & Cash-Flow — Frontend / MCP Integration Guide

**Audience:** the Claude (or human) working on the frontend / chat repo who needs to expose rental cash-flow analysis as a **tool executor + MCP tool**, the same way the CMA endpoint is consumed.
**Status:** backend pipelines live as of 2026-06-09. Backfill running; cron keeps it fresh.
**Backend scripts (this VPS):** `src/scripts/cma/build-rent-rates.py` (layer 1), `src/scripts/cma/build-cashflow.py` (layer 2).
**Companion docs:** `docs/cma/SUBDIVISION_AND_CITY_CMA.md`, `docs/cma/LISTING_CMA.md` — same precompute-then-`findOne` pattern.

---

## TL;DR

We pre-compute two things so chat queries like **"show me listings in Palm Desert that cash-flow with 20% down"** become a single Mongo `find + sort` — no request-time math:

1. **Going rate** — what properties actually *rent* for, per **subdivision** and per **ZIP** (area). Built from 100k+ closed (rented) leases.
2. **Cash-flow score** — for every active *sale* listing, the estimated rent + full investment math (financing + expenses) for standard down-payment scenarios.

```ts
// The whole MCP tool executor, essentially:
const listings = await UnifiedListing.find({
  city: "Palm Desert",
  "cashflowStats.scenarios.down20.cashflows": true,
}).sort({ "cashflowStats.scenarios.down20.monthlyCashflow": -1 }).limit(25);
```

Pair this with `cmaStats` (sale comps, already shipped) and you have buy-side + rent-side + cash-flow in one document.

---

## Where the data lives

| What | Collection | Field / shape | Built by |
|---|---|---|---|
| Going rate per community | `subdivisions` | `rentStats` (embedded) | `build-rent-rates.py` |
| Going rate per ZIP (area) | `rent_rates` | whole doc, keyed by `postalCode` (unique) | `build-rent-rates.py` |
| Cash-flow per sale listing | `unified_listings` | `cashflowStats` (embedded) | `build-cashflow.py` |

- **Database:** `admin` (app data lives in `admin` — see `docs/db/current-state.md`).
- **Mongoose models:** add `rentStats: { type: Schema.Types.Mixed }` to the Subdivision schema and `cashflowStats: { type: Schema.Types.Mixed }` to the UnifiedListing schema, and register a `RentRate` model on `rent_rates`. The Python builders use PyMongo and bypass Mongoose; without the schema declarations, `strict: true` silently drops the fields on `.lean()` reads.

### Why ZIP *and* subdivision

Only **~13%** of active sale listings carry a `subdivisionName`, but **~99%** carry a `postalCode`. So the ZIP `rent_rates` doc is the **primary** rent source for cash-flow matching; the subdivision `rentStats` is a more-local bonus used when present. This also closes the ~43% subdivision-orphan gap that limits the sale CMA.

---

## Schema 1 — `rentStats` (on subdivisions) and `rent_rates` docs (per ZIP)

Both targets carry the **same shape**; only `scope` / `geo` differ.

```ts
interface RentStats {
  lastUpdated: string;            // ISO-8601 UTC
  scope: "subdivision" | "zip";
  geo:
    | { subdivisionName: string; slug: string|null; city: string; county: string|null }
    | { postalCode: string; city: string|null };
  sampleWindow: { months: 24; startDate: string; endDate: string };

  // ── THE HEADLINE NUMBER ──────────────────────────────────────────────────
  goingRate: {
    monthlyRent: number | null;   // the defensible "going rate"
    annualRent: number | null;
    rentPerSqft: number | null;
    basis: "unfurnished-median" | "trimmed-all-median" | "insufficient";
    sampleSize: number;
  };

  // ── Distribution detail (all in-window) ──────────────────────────────────
  rented:      RentBlock;         // all rented leases
  unfurnished: RentBlock;         // clean annual basis
  furnished:   RentBlock & { note: string };  // "may include monthly/seasonal"
  active:      RentBlock;         // current for-rent ASKING supply

  byBedroom: Array<{              // match a subject property by bedroom count
    beds: number; bedsLabel: string;  // 5 => "5+"
    count: number;
    medianRent: number | null;
    p25Rent: number | null; p75Rent: number | null;
    medianRentPerSqft: number | null;
  }>;

  trends: {
    monthly: Array<{ month: string; count: number; medianRent: number|null }>;
    yoyMedianRentChangePct: number | null;
  };
  topRentedComps: string[];       // up to 5 listingKeys (join for detail)

  quality: { confidence: "good"|"fair"|"insufficient"; notes: string[] };
}

interface RentBlock {
  count: number;
  medianRent: number|null; avgRent: number|null;
  minRent: number|null; maxRent: number|null;
  p25Rent: number|null; p75Rent: number|null;
  medianRentPerSqft: number|null; medianSqft: number|null;
}
```

**The seasonal caveat is baked in.** `goingRate.monthlyRent` deliberately uses the **unfurnished** (long-term annual) median — what a buy-and-hold investor underwrites. When the unfurnished sample is thin, `basis` becomes `"trimmed-all-median"` (furnished/seasonal spikes trimmed at the 10th–90th pctile) and `quality.notes` says so. The `furnished` block is preserved separately for the seasonal/STR use case.

### Reading it

```ts
// Per ZIP:
const rate = await RentRate.findOne({ postalCode: "92211" });
// Per subdivision:
const sub = await Subdivision.findOne({ slug }, { rentStats: 1, name: 1, city: 1 });
```

---

## Schema 2 — `cashflowStats` (on `unified_listings`)

```ts
interface CashflowStats {
  lastUpdated: string;
  listPrice: number;

  rentEstimate: {
    monthlyRent: number;
    source: "subdivision-bedroom" | "subdivision-going-rate"
          | "zip-bedroom" | "zip-going-rate" | "zip-rent-per-sqft";
    confidence: "high" | "medium" | "low";
    geo: object;                  // the rentStats.geo it was matched from
  };

  capRatePct: number | null;      // NOI / price (NOI nets vacancy+mgmt+maint+tax+ins+HOA)
  noiAnnual: number;
  grossYieldPct: number | null;   // annual gross rent / price

  fixedCosts: {                   // monthly, debt-free — lets you re-derive any scenario
    grossRent: number; vacancy: number; propertyTax: number;
    insurance: number; hoa: number; management: number; maintenance: number;
    operatingExpensesMonthly: number;
  };

  scenarios: {                    // keyed downNN
    down20: ScenarioBlock;
    down25: ScenarioBlock;
  };

  assumptions: {                  // the inputs used — render as "what-if" defaults
    mortgageRate: number; loanTermYears: number;
    propertyTaxRate: number; insuranceRate: number;
    vacancyPct: number; managementPct: number; maintenancePct: number;
    closingCostPct: number; downScenarios: number[]; note: string;
  };
}

interface ScenarioBlock {
  downPct: number; downPayment: number; loanAmount: number;
  monthlyPI: number;              // principal + interest
  monthlyCashflow: number;        // effectiveRent − (tax+ins+HOA+mgmt+maint) − P&I
  annualCashflow: number;
  cashOnCashPct: number | null;   // annualCashflow / (down + closing costs)
  dscr: number | null;            // NOI / annual debt service
  cashflows: boolean;             // monthlyCashflow > 0  ← the filter flag
}
```

---

## The MCP tools / tool executors

Three tools cover the use cases. All are thin wrappers over Mongo — no LLM call, no external API.

### 1. `find_cashflowing_listings` — the headline tool

> *"show me listings in {area} that will cash-flow with {down}% down"*

```jsonc
{
  "name": "find_cashflowing_listings",
  "description": "Find active for-sale listings that produce positive rental cash flow in a given area, using pre-computed market rents and investment math.",
  "input_schema": {
    "type": "object",
    "properties": {
      "city":          { "type": "string", "description": "City name, e.g. 'Palm Desert'" },
      "postalCode":    { "type": "string", "description": "ZIP, alternative to city" },
      "subdivision":   { "type": "string" },
      "downPaymentPct":{ "type": "number", "default": 0.20, "description": "0.20 or 0.25 use precomputed scenarios; any other value is re-derived on the fly" },
      "minMonthlyCashflow": { "type": "number", "default": 0 },
      "maxPrice":      { "type": "number" },
      "beds":          { "type": "number" },
      "mortgageRate":  { "type": "number", "description": "Override; re-derives cash flow if set" },
      "sortBy":        { "type": "string", "enum": ["cashflow","capRate","cashOnCash","price"], "default": "cashflow" },
      "limit":         { "type": "number", "default": 25 }
    }
  }
}
```

**Executor — fast path (downPaymentPct is 0.20 or 0.25, no rate override):**

```ts
const key = `cashflowStats.scenarios.down${downPct*100}`;
const q: any = { propertyType: "A", standardStatus: { $in: ["Active","ComingSoon"] } };
if (city) q.city = city;
if (postalCode) q.postalCode = postalCode;
if (maxPrice) q.listPrice = { $lte: maxPrice };
if (beds) q.bedsTotal = beds;
q[`${key}.monthlyCashflow`] = { $gte: minMonthlyCashflow ?? 0 };

const sortField = {
  cashflow:   `${key}.monthlyCashflow`,
  capRate:    "cashflowStats.capRatePct",
  cashOnCash: `${key}.cashOnCashPct`,
  price:      "listPrice",
}[sortBy];

return UnifiedListing.find(q).sort({ [sortField]: -1 }).limit(limit);
```

**Executor — re-derive path (custom down % or rate).** No rebuild needed: `cashflowStats.fixedCosts` and `listPrice` are stored. Compute per candidate in JS and filter:

```ts
function deriveCashflow(cf, downPct, annualRate, termYears = 30) {
  const loan = cf.listPrice * (1 - downPct);
  const r = annualRate / 12, n = termYears * 12;
  const pi = r === 0 ? loan / n : loan * r * (1+r)**n / ((1+r)**n - 1);
  const fc = cf.fixedCosts;
  const monthly = (fc.grossRent - fc.vacancy)
                - (fc.propertyTax + fc.insurance + fc.hoa + fc.management + fc.maintenance)
                - pi;
  return { monthlyCashflow: Math.round(monthly), monthlyPI: Math.round(pi), cashflows: monthly > 0 };
}
```
Pre-narrow with the `down20` flag (anything cash-flowing at 20%/7% is a superset candidate for higher down %), then re-derive exact numbers. For custom rate, drop the pre-narrow and scan the city (a few thousand docs — still cheap with the index below).

### 2. `get_going_rate` — what does it rent for?

```ts
// ZIP first (always present), subdivision when asked specifically.
const rate = await RentRate.findOne({ postalCode });
// → rate.goingRate.monthlyRent, rate.byBedroom, rate.quality.confidence
```

### 3. `analyze_listing_cashflow` — single subject deep-dive

```ts
const l = await UnifiedListing.findOne({ listingKey }, { cashflowStats: 1, /* + display fields */ });
return l.cashflowStats;   // full breakdown incl. both scenarios + fixedCosts
```

For a custom down % / rate on one listing, run `deriveCashflow` above.

---

## Indexes to add (frontend/ops)

The filter+sort needs support or it collscans `unified_listings` (81k docs):

```js
db.unified_listings.createIndex(
  { city: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "city_cashflow20", partialFilterExpression: { propertyType: "A" } }
);
db.unified_listings.createIndex(
  { postalCode: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "zip_cashflow20" }
);
// rent_rates.postalCode unique index is created by the builder automatically.
```

---

## Freshness & fallback

- **Cron:** `build-rent-rates.py` runs twice weekly; `build-cashflow.py` runs right after (see `crontab.vps`). Rents move slowly, sale inventory turns faster — the cashflow pass is the one that matters for freshness.
- **`cashflowStats` absent** → listing added since the last run, or it's in an area with no rent signal (`rentEstimate` couldn't be formed). Render "cash-flow analysis unavailable" — do **not** invent a rent.
- **`rentEstimate.confidence === "low"`** (came from rent-per-sqft, not bedroom-matched comps) → surface a caveat; the number is an extrapolation.
- **`quality.confidence === "insufficient"`** on the underlying rentStats means too few rented comps — those areas won't produce a `cashflowStats` at all.

---

## Assumptions & caveats (surface these in the UI / chat answer)

- **Financing defaults** (overridable per query): 7.0% / 30-yr investment loan, 1.25% property tax, 0.4% insurance, 5% vacancy, 8% management, 5% maintenance/capex, 3% closing costs. These live in `cashflowStats.assumptions` — echo them so the user knows what "cash-flow" assumed.
- **Property tax is derived from list price** — there is no `taxAnnualAmount` in the MLS data. **Mello-Roos / special assessments are NOT modeled**, which understates costs in newer Riverside developments. Flag this for new-construction areas.
- **HOA** comes from `associationFee` (~87% of sale listings) normalized to monthly; assumed monthly when frequency is absent. Desert country-club HOAs are large and materially swing cash flow.
- **Seasonal rents:** the going rate is the long-term annual basis (see Schema 1). A furnished seasonal/STR strategy can earn far more than `goingRate` implies — that's a separate model; don't conflate.
- **The rent is an area estimate, not a per-unit appraisal.** It's bedroom- and ZIP/subdivision-matched, not condition/view-adjusted. Treat `monthlyCashflow` as a screen, not a guarantee.

---

## Related backend docs

- `src/scripts/cma/build-rent-rates.py` — going-rate builder (layer 1)
- `src/scripts/cma/build-cashflow.py` — cash-flow scorer (layer 2)
- `src/scripts/cma/build-rate-profiles.py` — per-listing seasonal rate extraction (complementary; powers STR/seasonal)
- `docs/cma/SUBDIVISION_AND_CITY_CMA.md`, `docs/cma/LISTING_CMA.md` — the sale-side CMA this mirrors
- `src/scripts/cma/README.md` — operational notes
