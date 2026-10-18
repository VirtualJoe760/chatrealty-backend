# Rental scripts

What things rent for. Each script writes one field, and later ones read earlier ones.

```
rental/
├── build-rate-profile.py            per-listing seasonal tiers -> rateProfile
├── build-going-rates.py             annual-lease medians -> rentStats, rent_rates
├── build-seasonal-rates.py          furnished ASKING rates -> rentStats.seasonal
├── build-closed-seasonal-rates.py   furnished ACHIEVED rates -> rentStats.closedSeasonal
├── build-pool-premium.py            private-pool rent lift -> rentStats.poolPremium
└── audit-rate-tiers.py              read-only tier-extraction diagnostic
```

The investor side consumes all of this; see `../investor/`. Architecture and the
tool contract are in `docs/cma/RENTAL_CASHFLOW.md`.

---
## Rental stack — `build-going-rates.py` & `../investor/build-cashflow.py`

Two layers that power the "show me listings in X that cash-flow with 20% down" chat tool.

**`build-going-rates.py`** — reads closed (rented) Residential-Lease listings (`closePrice` = the rent it leased at) and computes a `rentStats` "going rate" block, written to **both** `subdivisions.rentStats` and a ZIP-keyed **`rent_rates`** collection. ZIP coverage (~99% of listings carry a postalCode) closes the subdivision-orphan gap, which stands at 29.1% of the listings that carry a subdivisionName. Handles the desert seasonal-rent trap by segmenting furnished/unfurnished and using a trimmed-median fallback (there's no frequency/term field in the data).

```bash
python3 build-going-rates.py --subdivision "La Quinta Cove" --dry-run
python3 build-going-rates.py --zip 92253 --dry-run
python3 build-going-rates.py --city "Palm Desert"        # subs + ZIPs in the city
python3 build-going-rates.py --all                       # full run (cron)
python3 build-going-rates.py --all --subs-only           # skip ZIPs
python3 build-going-rates.py --all --zips-only           # skip subdivisions
```

**`../investor/build-cashflow.py`** — for each active sale listing, estimates rent from the layer-1 going rate (subdivision-bedroom > zip-bedroom > going-rate > rent-per-sqft), runs the financing + expense math for 20%/25%-down scenarios, and writes a `cashflowStats` block. Stores `fixedCosts` + `assumptions` so the tool executor can re-derive any down-payment / rate at query time without a rebuild. **Reads layer-1 output, so it must run after `build-going-rates.py`** (the cron chains them).

```bash
python3 ../investor/build-cashflow.py --city "Palm Desert" --dry-run
python3 ../investor/build-cashflow.py --all
python3 ../investor/build-cashflow.py --all --rate 0.0675 --skip-fresh-hours 60
```

### Two seasonal layers, deliberately kept apart

`build-seasonal-rates.py` reads `rateProfile` off **active** leases. Only ~4.75%
of those profiles parse as "tiered", so it runs on roughly 750 listings
valley-wide — which is why 318 of 333 subdivisions come back "insufficient" for
seasonal and the PDCC showcase's 4-bedroom headline rested on 2 comps.

`build-closed-seasonal-rates.py` (P4.6) adds a second, independent source: the
254,809 **closed** leases back to 2020-08-28, where `closePrice` is rent
actually achieved rather than rent asked. It writes a separate
`closedSeasonal` block and never touches `seasonal`, so the two can be compared
instead of silently blended.

They are not interchangeable, and the split is not stylistic:

| | active tiered layer | closed-lease layer |
|---|---|---|
| rate LEVEL | asking | achieved ✅ |
| sample | ~750 valley-wide | 254,809 statewide |
| peak MONTHS | real, parsed from remarks ✅ | **unavailable** |

The closed layer cannot supply peak months. Close dates record when a deal was
struck, not which months it covers — seasonal leases are booked well ahead — so
the high-price tail is the same size in season and out (92211 3bd: 13.0%
in-season vs 12.8% off-season). It therefore takes `peakMonths` from the active
layer when one exists and labels the source.

A seasonal lease is selected by PRICE — a close at or above 2x its own
(ZIP, beds) cell median — with the `furnished` field as corroboration, not as
the selector. "Furnished ⇒ seasonal" was tried and is wrong: 85% of furnished
closes are furnished *annual* rentals priced at $3,450 against a $3,250
unfurnished median, and selecting on the flag collapsed ZIP 92211 to a $3,500
"seasonal" rate. Price selection puts it at $10,000 against a $2,800 long-term
baseline. The flag's value is validation: on rows carrying both signals, 14.8%
of furnished closes land in the tail against 0.6% of unfurnished — a 25x
enrichment, which is what licenses using price alone where the flag is absent
(it is ~97% null overall, but present on 96% of GPS/Coachella Valley closes).

```bash
python3 build-closed-seasonal-rates.py --zip 92211 --dry-run
python3 build-closed-seasonal-rates.py --slug palm-desert-country-club --dry-run
python3 build-closed-seasonal-rates.py --all
```

**The threshold truncates its own sample, deliberately.** Nothing below 2x can
enter the cohort, so `seasonalRate` is not "the typical seasonal rent" — it is
what leases priced at a clearly seasonal level achieved, and it reads high next
to the active layer (92211: $10,000 achieved vs $6,000 median tiered asking).
Both are correct answers to different questions. Loosening the multiple drags
furnished-annual rentals back in, which is the failure the selector exists to
prevent.

Unit discipline is the same as the active layer: `seasonalRate` is an in-season
MONTHLY rate. Only `annualizedMonthlyBlended` (peak months at the seasonal
rate, the rest at the long-term baseline) and `annualizedMonthlyPeakOnly`
(the rest at zero — the floor) may feed cash-flow math.

### Both layers reach the cash-flow stack (P4.7)

`build-cashflow.py` scores them as two separate strategies per listing —
`strategies.seasonal` (asking) and `strategies.closedSeasonal` (achieved) —
sharing one code path and one set of `SEASONAL_OVERRIDES`, so they can differ
only by their rent. `build-cashflow-profile.py` then composes both into
`cashflowProfile`, with `rents.seasonalDivergence` recording the peak-to-peak
ratio once so no consumer re-derives it (and risks comparing an asking peak
against an achieved blend).

The closed layer covers **6x more inventory**: 18,835 active listings get an
achieved-seasonal score against 3,159 on the asking layer, because tiered
active listings are scarce while closed leases are not.

**Expect the achieved strategy to cash-flow on fewer listings anyway**, and know
why before treating it as a discrepancy. The two annualized rents price
off-peak months differently, and off-peak is 8 of 12:

| | off-peak months priced at |
|---|---|
| `seasonal.annualizedMonthly` | the listing's own furnished ASKING tier (92211: $3,500) |
| `closedSeasonal.annualizedMonthlyBlended` | the achieved LONG-TERM baseline (92211: $2,800 — unfurnished annual) |

So the achieved layer only wins where the peak lift outruns that off-peak drag.
92211 3bd: $5,400 achieved vs $4,704 asking (peak $10,000 vs $6,000 wins).
92211 4bd: $5,800 vs $6,229 (peak ties at $10,000, so cheaper off-peak decides
it). Nobody has achieved-furnished off-peak rates — a furnished off-peak lease
is rare enough that inferring one would be invention — so the conservative
blend is the honest one.

### The Tue/Fri 2 AM chain, and why order matters

Five stages, chained with `&&` so none reads a half-built input (~3.5 min total,
measured 2026-10-14):

    build-rent-rates -> build-pool-premium -> build-cashflow
      -> build-inventory-profile -> build-cashflow-profile

`build-cashflow` additionally needs the two seasonal layers, which run in the
separate 1:30 AM job ahead of it.

**Stages 2, 4 and 5 were never scheduled before 2026-10-14.** `cashflowProfile`
— the single `findOne` the MCP tools serve — was only ever built by hand, so it
drifted out of agreement with the `cashflowStats` it is supposed to be a median
of, while the listing-level data under it refreshed twice a week. Exactly the
staleness the seasonal layers had before P4.6 scheduled them. Add a stage to
this stack and you must schedule it here in dependency order, or it goes stale
the same silent way.

---

Financing defaults (all CLI-overridable, echoed into `cashflowStats.assumptions`): 7.0%/30-yr loan, 1.25% tax (derived from price — no `taxAnnualAmount` in MLS; Mello-Roos not modeled), 0.4% insurance, 5% vacancy, 8% management, 5% maintenance, 3% closing.

---
