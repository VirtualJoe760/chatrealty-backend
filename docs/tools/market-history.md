# Market history and financing assumptions

Two stored things that the investment-analysis stages are built on top of. They
are grouped here because neither is a listing field and both exist to be read
rather than recomputed.

- **`market_history`** — annual medians per area, permanent, no TTL.
- **`loan_products`** — the down-payment ladder and what mortgage insurance
  costs, stored rather than coded.

## Why `market_history` exists

`unified_closed_listings` has a five-year rolling TTL. That window is the
**feed's horizon**, not a shortfall: Spark serves five years back and no more,
and our own baseline starts in August 2020 because that is as far as anyone can
go. There is no version of this where the collection holds ten years.

The consequence is that the view backwards never gets longer. Today it reaches
only into the last quarter of 2021 — nine of that year's twelve months have
already aged out — and in 2030 it will reach 2025, no further. Any statement about long-run appreciation made
from the live collection alone is permanently capped at five years, and five
years that happen to contain a historic run-up are the worst possible basis for
a long-run estimate.

`historical/build-market-history.py` fixes that by writing the aggregate down
before the detail disappears. Individual sales are disposable; the annual median
for a ZIP is not. The archive grows by one year per year and never shrinks.

It is cheap insurance, not an emergency. Twice a year is plenty. Nothing breaks
if a run is late — but a year the TTL takes before it is archived is gone for
good, and no amount of later effort brings it back.

## Shape

One document per area, property type and year. `_id` is
`<scope>:<key>:<propertyType>:<year>`, which makes every write idempotent.

| Field | Meaning |
|---|---|
| `scope` | `zip`, `city` or `subdivision` |
| `key` | `92260`, `palm desert`, or `palm desert\|pga west` |
| `label` | human-readable name where the feed carries one |
| `propertyType` | `A` sale, `B` lease |
| `year` | calendar year of close |
| `closedCount` | transactions behind every figure below |
| `medianClosePrice` | the headline number |
| `meanClosePrice`, `p25ClosePrice`, `p75ClosePrice` | spread, for a band rather than a point |
| `medianPricePerSqft` | the mix-resistant one; prefer it for trend |
| `medianLivingArea` | what changed in the mix, if ppsf and price disagree |
| `medianDaysOnMarket` | absorption; **derived**, see below |
| `medianCloseToListRatio` | close ÷ original list |
| `sampleSizes` | per-metric counts; they differ, because not every closing carries sqft or DOM |
| `monthsCovered` | months **this area** had a closing in, 1–12 — a thin-area signal |
| `yearMonthsCovered` | months **the year** has any data for, 1–12 |
| `partialYear` | true when the year itself is truncated or still running |
| `quality.confidence` | `good` ≥ 30 closings, `fair` ≥ 10, else `low` |
| `archivedAt` | when this row was written |
| `note` | the estimate disclaimer, stored so a reader cannot drop it |

The scopes overlap deliberately. A sale in a named subdivision counts toward its
ZIP and its city as well, because the three answer different questions and a
subdivision rarely has enough volume on its own.

### Reading it

Prefer `medianPricePerSqft` over `medianClosePrice` for trend. A median price
moves when the *mix* of what sold moves — a year where more large homes traded
reads as appreciation that no individual owner experienced. Per-square-foot
absorbs much of that, and `medianLivingArea` is the check: if ppsf and price
disagree, the mix changed.

Never compare a `partialYear` row to a full one. The market here is seasonal
enough that a year weighted toward the January–April selling season reads
materially higher than the same year complete.

`partialYear` describes the **year**, not the area. It is set from
`yearMonthsCovered` — how much of that calendar year the collection holds at
all — so every area in a truncated or in-progress year carries it, and no area
in a complete year does. `monthsCovered` is the separate, per-area number, and a
subdivision showing 7 of 12 months is a thin area rather than a gap in the data.
Keeping these apart matters: tying `partialYear` to the per-area count would
flag most subdivision rows in a perfectly complete year and invite a reader to
throw them away.

`medianDaysOnMarket` is derived, not reported. `daysOnMarket` exists in the
schema but this feed never populates it on a closed record — it is absent from
every closed document — so the archive computes `closeDate - onMarketDate`
instead, falling back to `listingContractDate`. That is the same rule
`src/scripts/historical/README.md` already prescribes for closed comps. It
measures time from going on market to close, so it includes escrow.

Check `closedCount` before quoting anything. `low` confidence at the
subdivision scope is common and normal; roll up to the ZIP rather than quoting a
median built on six sales.

At the subdivision scope, check name coverage before trusting a recent year.
CRMLS stopped publishing `SubdivisionName` through Spark around September 2025 —
coverage on closings fell from ~26% (2024) to ~11% (2026), a cliff between
August and September 2025, with no substitute field available. The Greater Palm
Springs feed still populates it, so Coachella Valley is unaffected; CRMLS
territory is not. `rank-subdivisions.py` prints the ratio and warns below 20%.

At the subdivision scope, exclude placeholder names before ranking anything.
~31% of closings that carry a `subdivisionName` carry `Other`, `Not Applicable`
or `N/A`, which mean "none entered" — in a volume ranking they take 8 of the top
10 slots. `src/scripts/historical/rank-subdivisions.py` is the read-only report
that does this correctly and documents the blocklist.

### The guard that makes it worth keeping

A re-run must never make the archive worse. Once the TTL has eaten part of a
year, recomputing it produces fewer closings than the archived figure was built
from, and a plain upsert would replace a complete 2022 with a gutted one — the
exact loss the archive exists to prevent.

So any write that would **lower** an existing `closedCount` is refused, reported
with both numbers, and left alone. `--force` is the only way past it, and the
only good reason to use it is a correction upstream that genuinely changed the
underlying data.

## `loan_products`

Financing assumptions, stored in two documents because they are lender pricing
rather than law — they move, and changing one should not need a deploy.
`utilities/set-loan-products.py` reads them and `--seed` writes the defaults.

### `down_payment_ladder`

```
rungs:    0%  3%  5%  10%  15%  20%  25%  30%  40%  50%  60%  75%  80%
showcase: 3%  10%  20%  50%  75%
```

**One continuous ladder, no eligibility gating.** 0% is on it because a VA buyer
really can put nothing down, and 80% is the top because past that the loan stops
driving the answer. There is no occupancy rule, no loan limit and no
owner-occupied/investor split, because none of that is our job: we are not
writing the loan, we are showing what each down payment does to the cash flow.
Which rung applies to a given buyer is theirs to know.

`showcase` is display order only — the rungs a listing card leads with so it is
not thirteen rows wide. Everything else stays stored and reachable.

### `mortgage_insurance`

Mortgage insurance appears here as a **dollar cost**, never as a rule about who
may borrow. Annual rate as a fraction of the loan balance, banded by LTV at
origination (`minLtv` exclusive, `maxLtv` inclusive, so each rung lands in
exactly one band):

| LTV | Cost |
|---|---|
| 97–100% | no monthly MI; 2.15% upfront funding fee |
| 95–97% | 1.10% / yr |
| 90–95% | 0.62% / yr |
| 85–90% | 0.41% / yr |
| 80–85% | 0.28% / yr |
| ≤ 80% | none |

`dropOffLtv` is 0.78. MI comes off once the amortized balance reaches 78% of the
original price, and a projection that charges it for all thirty years overstates
the cost of a low-down-payment buy — which is the whole question the ladder
asks.

## These are estimates

Every figure from either collection is an estimate and says so in its own
`note`. Appreciation medians describe an area, not a property. Financing figures
leave out state and local fees, vary by lender and credit profile, and are not
quotes or eligibility determinations. The disclaimer is stored alongside the
numbers specifically so a read surface cannot show one without the other.
