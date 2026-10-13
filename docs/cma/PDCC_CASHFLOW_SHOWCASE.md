# Showcase — Cash-Flow Rates for Palm Desert Country Club

**Generated:** 2026-10-13 17:35 UTC (10:35 AM PDT)
**Subdivision:** Palm Desert Country Club (`palm-desert-country-club`), Palm Desert, Riverside County
**Acceptance goal for the overnight build:** showcase cash-flow rates for PDCC.

This is the output of a single `findOne` on `subdivisions` by slug. Every number
below is read from the stored `cashflowProfile` — no request-time aggregation and
no arithmetic at read time. That was the point of the build.

---

## The answer, per strategy

The three rental strategies are assessed separately, because they are different
businesses with different cost structures.

| Strategy | Determination | Break-even down payment | Best listing needs |
|---|---|---|---|
| **Seasonal (furnished, Nov–Apr)** | Does not cash-flow at 20% down | **47.1%** | **23.1%** |
| **Long-term (annual lease)** | Does not cash-flow at 20% down | **65.4%** | 41.9% |
| **Short-term (nightly)** | **Unclear — not modelled** | n/a | n/a |

**Lead finding:** seasonal is the materially better strategy here — it cuts the
required down payment from 65.4% to 47.1%, and the best single listing in the
subdivision gets to 23.1%, which is within reach of a conventional investor
down payment. Long-term is not close at current rates.

**Short-term is unclear, not negative.** No MLS source carries nightly rates, so
a number here would be invention. To price it you would need to verify the HOA
minimum-lease term and the City of Palm Desert short-term-rental ordinance.
PDCC is not gated (0% of inventory), and 57% of pools are association-owned
rather than private, both of which bear on short-term viability.

### The strongest cell: 4-bedroom, seasonal

| | 2bd | 3bd | **4bd** |
|---|---|---|---|
| Active listings | 3 | 9 | **3** |
| Archetype price | $418,000 | $545,500 | **$524,900** |
| Seasonal break-even down | 47.1% | 53.0% | **28.7%** |
| Seasonal cap rate | 4.40% | 3.91% | **5.92%** |
| Monthly cash flow @ 20% down | −$783 | −$1,207 | **−$302** |
| DSCR @ 20% down | 0.66 | 0.59 | **0.89** |
| Long-term break-even down | 58.7% | 65.4% | 72.1% |

The 4-bedroom seasonal case is the one worth a second look: **−$302/month at 20%
down, DSCR 0.89, break-even at 28.7%.** It is driven by the 4bd seasonal rate
($11,275 peak / $4,950 off-peak, annualized $6,229) — but note that rate rests on
**only 2 lease comps**. That is the thinnest number in this report and the one to
verify before acting on it.

---

## Inventory (15 active sale listings)

- **Median list price:** $529,000 · range $180,000 – $769,000
- **Median size:** 1,501 sqft · **$323.29/sqft**
- **Average days on market:** 94
- **Dominant subtype:** Single Family Residence
- **Bedroom mix:** 2bd ×3 (20%) · 3bd ×9 (60%) · 4bd ×3 (20%)

**Pools** — this matters for the strategy call:

| | Count | Share |
|---|---|---|
| Private pool | 5 | 35.7% |
| Community/association pool only | 8 | 57.1% |
| No pool | 1 | |
| Unknown | 1 | |
| **Any pool** | | **92.9%** |

Spa 100% (13 of 15 with data) · Gated 0% (15 of 15 with data).

Prevalences are stored as `{pct, withData, total}` so an area with sparse data
cannot be misread as an area with a genuinely low rate.

---

## Rents

### Long-term — local, good confidence
Median unfurnished asking rent **$2,500/mo** (n=12), from PDCC itself.

| Beds | Comps | Median rent | P25 – P75 | $/sqft |
|---|---|---|---|---|
| 1bd | 4 | $1,722.50 | $1,658.75 – $1,750 | 2.19 |
| 2bd | 7 | $2,200.00 | $2,147.50 – $2,450 | 2.01 |
| 3bd | 14 | $2,712.50 | $2,612.50 – $2,872.50 | 1.89 |

### Seasonal — from ZIP 92211, not PDCC itself
PDCC had too few tiered (seasonally-priced) lease comps to stand alone, so the
seasonal block falls back to its dominant ZIP, **92211**, and says so in
`rents.seasonal.source`. Basis: tiered-median, n=22. Peak months **Jan–Apr**.
157 flat-rate leases were excluded from the seasonal medians.

| Beds | Comps | Peak | Off-peak | Annualized monthly |
|---|---|---|---|---|
| ZIP overall | 22 | $6,000 | $3,500 | $4,392 |
| 2bd | 8 | $4,950 | $2,850 | $3,754 |
| 3bd | 12 | $7,000 | $3,850 | $4,704 |
| 4bd | 2 | $11,275 | $4,950 | $6,229 |

**Only `annualizedMonthly` feeds the seasonal economics.** The raw peak rate is
never used as if it were collected twelve months a year — that would be the
single easiest way to manufacture a fake cash-flowing property.

### Private-pool premium — unavailable here, honestly
`insufficient`. No bedroom+subtype cell had enough private-pool *and* no-pool
leases to isolate a premium. This is expected in country-club areas, where
nearly every pool is association-owned: there is no clean private-vs-none
contrast to measure. Reported as unavailable rather than estimated.

---

## Economics and assumptions

**Mortgage rate: 7.40%**, live from API Ninjas, as of 2026-10-13 07:42 UTC.

| Assumption | Long-term | Seasonal |
|---|---|---|
| Property tax | 1.25% | 1.25% |
| Insurance | 0.40% | 0.40% |
| Vacancy | 5% | **15%** |
| Management | 8% | **20%** |
| Maintenance | 5% | **8%** |
| Loan term | 30 yr | 30 yr |

The seasonal overrides are the heart of the model: a furnished seasonal rental
carries roughly triple the vacancy, 2.5× the management load, and higher
turnover maintenance. Applying long-term expense ratios to a seasonal rate would
overstate returns badly. Even after those heavier costs, seasonal still wins
here — which is what makes the finding credible rather than an artifact.

| Rollup metric | Long-term | Seasonal |
|---|---|---|
| Listings scored | 15 | 15 |
| Rent used | $2,712.50 | $4,704.00 |
| Break-even down | 65.44% | 47.06% |
| Best listing break-even | 41.90% | 23.10% |
| Monthly cash flow @ 20% | −$1,664.26 | −$841.31 |
| Cash-on-cash @ 20% | −16.41% | −9.78% |
| DSCR @ 20% | 0.43 | 0.66 |
| Cap rate | 2.87% | 4.40% |
| Listings clearing @ 20% down | 0 | 0 |
| Negative at any down payment | 0 | 0 |

Nothing in PDCC is negative at *every* down payment — all 15 listings do clear
with enough equity. The constraint is leverage, not the underlying rent.

---

## Reading note: `minBreakEvenDownPct` vs `capRatePct`

These two fields describe **different listings** and must not be read as one
property. `minBreakEvenDownPct` is the best single listing in the geography;
`capRatePct` and `breakEvenDownPct` are the median listing.

Indian Palms (Indio) is the clean example: median cap rate 5.21%, median listing
break-even 37.3%, but `minBreakEvenDownPct` 0.0 — because its best unit is a
$240k 1bd at an 11.66% seasonal cap, not the $486k median. A 0.0 sitting beside
a 5.21% cap looks like a solver bug and is not one: `break_even_down()` returns
`0.0` only when the maximum supportable loan meets or exceeds the price, and
returns `None` — never zero — when no down payment works. Any consumer of this
data, MCP tool or UI, should label which listing a figure refers to.

---

## Valley context — where it does clear at 20% down

PDCC's negative verdict is not a pipeline failure; it is a true statement about a
country-club subdivision at a 7.40% mortgage rate. For calibration:

- **741 subdivisions** and **836 cities** now carry a `cashflowProfile`.
- **112 of 741** subdivisions have at least one listing clearing **seasonally** at
  20% down; **57** have at least one clearing **long-term**.
- Valley-wide, **559 of 3,159** seasonally-priced listings clear at 20% down
  (17.7%) versus **869 of 41,890** long-term (2.1%) — seasonal is roughly **8×**
  more likely to cash-flow.
- On seasonal break-even, PDCC ranks mid-field: **116 of 333** subdivisions with
  seasonal data do better than its 23.1%.

Subdivisions with the most seasonally cash-flowing inventory:

| Subdivision | City | Listings clearing @20% | Median price | Seasonal cap |
|---|---|---|---|---|
| Outdoor Resorts Country Club | Indio | 78 | $135,000 | 19.93% |
| Palm Springs Villas I | Palm Springs | 15 | $185,000 | 21.59% |
| Desert Park Estates | Palm Springs | 13 | $799,000 | 6.22% |
| Demuth Park | Palm Springs | 13 | $760,000 | 9.03% |
| Indian Palms | Indio | 10 | $486,000 | 5.21% |
| Canyon Sands | Palm Springs | 10 | $441,500 | 14.88% |
| Mountain Cove | Indian Wells | 9 | $359,500 | 6.55% |
| Deep Canyon Tennis Club | Palm Desert | 8 | $352,000 | 7.43% |
| Esprit | Palm Springs | 8 | $232,500 | 30.82% |

The pattern: lower-priced attached and resort-zoned product carries seasonal
cash flow; higher-priced country-club detached product does not, at this rate.
Deep Canyon Tennis Club is the nearest in-city comparison to PDCC.

---

## Provenance

Composed from six stages, each writing its own fields:

| Stage | Script | Writes |
|---|---|---|
| Price/DOM aggregates | `build-subdivision-cma.py` | `cmaStats.active` |
| Bed mix + prevalences | `build-inventory-profile.py` | `inventoryProfile`, `cmaStats.active.bedMix` |
| Long-term going rate | `build-rent-rates.py` | `rentStats`, `rent_rates` |
| Seasonal rates | `build-seasonal-rates.py` | `rentStats.seasonal`, `dominantPostalCode` |
| Private-pool premium | `build-pool-premium.py` | `rentStats.poolPremium` |
| Per-listing economics | `build-cashflow.py` | `cashflowStats.strategies`, `breakEvenDownPct` |
| **This rollup** | `build-cashflow-profile.py` | `subdivisions.cashflowProfile` |

The rollup **composes only** — every figure is a median over per-listing
`cashflowStats` already stored, so the subdivision page can never disagree with
the listing detail a user drills into.

Hand-verified: PDCC 3bd at $694,900 → NOI/12 = $1,345.80 P&I budget → $194,320
maximum loan → 72.03% break-even. Exact against the stored value.

---

## Caveats to carry forward

1. **4bd seasonal rests on 2 lease comps.** The most attractive number here is
   also the thinnest. Verify before acting.
2. **Seasonal is ZIP-level (92211), not PDCC-level.** Directionally sound, but it
   does not capture PDCC-specific positioning.
3. **No private-pool premium for this area.** Community pools dominate, so a
   private pool's rent lift cannot be isolated — it is likely real but unmeasured,
   and would improve the 35.7% of inventory that has one.
4. **Short-term unpriced.** Requires HOA minimum-lease terms and the Palm Desert
   STR ordinance, neither of which is in the MLS.
5. **Thin-cell premiums elsewhere.** Subdivision-level pool premiums with n<20
   still show large lifts (e.g. Newport Beach +193%, flagged "fair"). The rollup
   prefers ZIP over subdivision for exactly this reason; a city tier would help.
