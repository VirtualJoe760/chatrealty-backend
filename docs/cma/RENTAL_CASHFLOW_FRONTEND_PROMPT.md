# Frontend Handoff Prompt — Rental Cash-Flow Tool Executor + MCP Tool

Copy everything below the line into a fresh Claude session **in the frontend / chat repo**.
It is self-contained (the backend is a separate repo), so the schemas and query
patterns are embedded inline.

---

You are working in the jpsrealtor **frontend / Next.js** repo. The backend team just shipped a
**rental cash-flow data layer** (precomputed into MongoDB, same pattern as our existing CMA
system). Your job: expose it to the AI chat as a **tool executor + MCP tool** so a user can ask
*"show me listings in Palm Desert that will cash-flow with 20% down"* and get real results.

This is a **pure read** feature — no request-time math, no external API. Mirror exactly how our
existing **CMA tool executor / MCP tool** is wired (find that code first and copy its structure,
registration, auth, and error handling). Do not invent a new pattern.

## What the backend already did (don't rebuild any of this)

Three things are precomputed in the `admin` database and refreshed by cron (Tue + Fri):

1. **`rent_rates` collection** — one doc per ZIP (keyed by unique `postalCode`). The market
   "going rate" for rentals in that area.
2. **`subdivisions.rentStats`** — same shape as a `rent_rates` doc, embedded on each subdivision.
3. **`unified_listings.cashflowStats`** — on every active **sale** listing: the estimated rent +
   full investment math (financing + expenses) for 20%/25%-down scenarios.

> Why ZIP matters: only ~13% of sale listings carry a `subdivisionName`, but ~99% carry a
> `postalCode`. So rent matching is ZIP-first. The cash-flow scorer already did that matching;
> you just read `cashflowStats`.

## Step 1 — Mongoose model additions

The Python builders use PyMongo and bypass Mongoose, so `strict: true` will silently drop these
fields on `.lean()` reads unless you declare them:

```ts
// UnifiedListing schema:
cashflowStats: { type: Schema.Types.Mixed },
// Subdivision schema:
rentStats: { type: Schema.Types.Mixed },
// New model on the `rent_rates` collection:
const RentRate = model("RentRate", new Schema({}, { strict: false, collection: "rent_rates" }));
```

## Step 2 — Indexes (run once against the cluster)

The filter+sort collscans 81k listings without these:

```js
db.unified_listings.createIndex(
  { city: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "city_cashflow20" }
);
db.unified_listings.createIndex(
  { postalCode: 1, "cashflowStats.scenarios.down20.monthlyCashflow": -1 },
  { name: "zip_cashflow20" }
);
```

## Step 3 — `cashflowStats` shape (what you read off each listing)

```ts
interface CashflowStats {
  lastUpdated: string;
  listPrice: number;
  rentEstimate: {
    monthlyRent: number;
    source: "subdivision-bedroom" | "subdivision-going-rate"
          | "zip-bedroom" | "zip-going-rate" | "zip-rent-per-sqft";
    confidence: "high" | "medium" | "low";
    geo: object;
  };
  capRatePct: number | null;        // NOI / price
  noiAnnual: number;
  grossYieldPct: number | null;
  fixedCosts: {                     // monthly, DEBT-FREE — lets you re-derive any scenario
    grossRent: number; vacancy: number; propertyTax: number;
    insurance: number; hoa: number; management: number; maintenance: number;
    operatingExpensesMonthly: number;
  };
  scenarios: {
    down20: ScenarioBlock;
    down25: ScenarioBlock;
  };
  assumptions: {                    // echo these so the user knows what "cash-flow" assumed
    mortgageRate: number; loanTermYears: number; propertyTaxRate: number;
    insuranceRate: number; vacancyPct: number; managementPct: number;
    maintenancePct: number; closingCostPct: number; downScenarios: number[]; note: string;
  };
}
interface ScenarioBlock {
  downPct: number; downPayment: number; loanAmount: number; monthlyPI: number;
  monthlyCashflow: number; annualCashflow: number;
  cashOnCashPct: number | null; dscr: number | null;
  cashflows: boolean;               // monthlyCashflow > 0  ← the filter flag
}
```

`rent_rates` / `rentStats` doc shape (only the fields you'll likely surface):
```ts
{ goingRate: { monthlyRent, annualRent, rentPerSqft, basis, sampleSize },
  byBedroom: [{ beds, bedsLabel, count, medianRent, p25Rent, p75Rent, medianRentPerSqft }],
  rented: {...}, unfurnished: {...}, furnished: {... , note},
  quality: { confidence: "good"|"fair"|"insufficient", notes: [] },
  geo: { postalCode|subdivisionName, city }, sampleWindow: { months: 24 } }
```

## Step 4 — Build THREE tools (register them the same way the CMA tool is registered)

### Tool 1 (primary): `find_cashflowing_listings`

```jsonc
{
  "name": "find_cashflowing_listings",
  "description": "Find active for-sale listings that produce positive rental cash flow in a given area, using pre-computed market rents and investment math. Use when a user asks for cash-flowing / cash-flow-positive / investment properties in a place.",
  "input_schema": {
    "type": "object",
    "properties": {
      "city":          { "type": "string" },
      "postalCode":    { "type": "string" },
      "subdivision":   { "type": "string" },
      "downPaymentPct":{ "type": "number", "default": 0.20 },
      "minMonthlyCashflow": { "type": "number", "default": 0 },
      "maxPrice":      { "type": "number" },
      "beds":          { "type": "number" },
      "mortgageRate":  { "type": "number" },
      "sortBy":        { "type": "string", "enum": ["cashflow","capRate","cashOnCash","price"], "default": "cashflow" },
      "limit":         { "type": "number", "default": 25 }
    }
  }
}
```

**Executor — fast path** (downPaymentPct ∈ {0.20, 0.25} and no `mortgageRate` override):

```ts
const dk = `cashflowStats.scenarios.down${Math.round(downPaymentPct*100)}`;
const q: any = { propertyType: "A", standardStatus: { $in: ["Active","ComingSoon"] } };
if (city)       q.city = city;
if (postalCode) q.postalCode = postalCode;
if (maxPrice)   q.listPrice = { $lte: maxPrice };
if (beds)       q.bedsTotal = beds;
q[`${dk}.monthlyCashflow`] = { $gte: minMonthlyCashflow ?? 0 };

const sortField = {
  cashflow:   `${dk}.monthlyCashflow`,
  capRate:    "cashflowStats.capRatePct",
  cashOnCash: `${dk}.cashOnCashPct`,
  price:      "listPrice",
}[sortBy];

const rows = await UnifiedListing.find(q)
  .sort({ [sortField]: -1 })
  .limit(limit)
  .select("listingKey slugAddress address city listPrice bedsTotal bathsTotal livingArea cashflowStats")
  .lean();
```

**Executor — re-derive path** (custom down % or `mortgageRate`). No rebuild needed — `fixedCosts`
+ `listPrice` are stored. Pre-narrow on the `down20` flag (a superset of higher-down-% winners),
then recompute exact numbers in JS:

```ts
function deriveCashflow(cf, downPct, annualRate, termYears = 30) {
  const loan = cf.listPrice * (1 - downPct);
  const r = annualRate / 12, n = termYears * 12;
  const pi = r === 0 ? loan/n : loan * r * (1+r)**n / ((1+r)**n - 1);
  const f = cf.fixedCosts;
  const monthly = (f.grossRent - f.vacancy)
                - (f.propertyTax + f.insurance + f.hoa + f.management + f.maintenance) - pi;
  const cashIn = cf.listPrice * downPct + cf.listPrice * cf.assumptions.closingCostPct;
  return {
    monthlyCashflow: Math.round(monthly),
    annualCashflow: Math.round(monthly*12),
    cashOnCashPct: cashIn ? +(monthly*12/cashIn*100).toFixed(2) : null,
    monthlyPI: Math.round(pi), cashflows: monthly > 0,
  };
}
// rate = mortgageRate ?? candidate.cashflowStats.assumptions.mortgageRate
```

For a custom **rate**, drop the `down20` pre-narrow and scan the city (a few thousand docs — fine
with the indexes above), re-derive each, filter `cashflows`, sort, slice to `limit`.

### Tool 2: `get_going_rate`
```ts
// ZIP first (near-universal coverage). Returns goingRate + byBedroom + quality.confidence.
const rate = await RentRate.findOne({ postalCode }).lean();
// If a subdivision was named: Subdivision.findOne({ slug or name }, { rentStats: 1 })
```

### Tool 3: `analyze_listing_cashflow`
```ts
const l = await UnifiedListing.findOne({ listingKey }, { cashflowStats: 1, /* +display */ }).lean();
return l.cashflowStats;   // full breakdown, both scenarios + fixedCosts; re-derive for custom inputs
```

## Step 5 — How the chat should present results

- Lead with the count and the assumptions: *"12 listings in Palm Desert cash-flow at 20% down,
  assuming a 7% / 30-yr loan, 5% vacancy, 8% management."* (Read these from
  `cashflowStats.assumptions` — never hard-code them.)
- Per listing show: address, price, **estimated rent** + its `rentEstimate.confidence`, **monthly
  cash flow**, **cap rate**, **cash-on-cash**. Link to the listing detail page.
- If `rentEstimate.confidence === "low"`, caveat that the rent is an area extrapolation.

## Caveats to surface (do not bury these)

- **Property tax is derived from list price** — there's no tax field in the MLS feed.
  **Mello-Roos / special assessments are NOT modeled** → understates costs on new Riverside builds.
- **HOA** comes from `associationFee` (normalized to monthly). Desert country-club HOAs are large.
- The going rate is **long-term annual** rent. A furnished **seasonal/STR** strategy can earn much
  more — that's a different model; don't conflate. (`furnished` stats exist separately if asked.)
- `cashflowStats` **absent** → listing added since last cron run, or no rent signal for its area →
  render "cash-flow analysis unavailable"; do NOT invent a rent.

## Acceptance test

Once wired, this chat turn should work end-to-end and return real rows sorted by cash flow:
> "Show me listings in Palm Desert that will cash-flow with 20% down."

And the custom-input path:
> "What about with 30% down at a 6.5% rate?"  → re-derives without a rebuild.

Backend reference (if you can access the backend repo): `docs/cma/RENTAL_CASHFLOW.md` is the full
contract; `src/scripts/cma/build-rent-rates.py` + `build-cashflow.py` are the producers.
