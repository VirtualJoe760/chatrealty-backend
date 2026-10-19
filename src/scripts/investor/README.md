# Investor scripts

Whether a property is a good buy. These read the rental figures from
`../rental/` and turn them into financing math.

```
investor/
├── build-cashflow.py           per-listing scoring -> cashflowStats
├── build-inventory-profile.py  bed mix + pool prevalence -> inventoryProfile
└── build-cashflow-profile.py   the area rollup -> cashflowProfile
```

Run them in that order; the cron chains them after the rental stack.

`build-cashflow.py` holds all the money math — the live mortgage-rate fetch,
the investor-rate spread, amortized payment, its inverse, and the break-even
down-payment solve. There is no separate mortgage calculator.
`docs/scripts.md` lists those functions and `docs/cma/RENTAL_CASHFLOW.md` has
the architecture.

The rate is two numbers. `frm_30` from Freddie Mac is an owner-occupied rate,
so `investorRateSpread` is added on top and `mortgageRate` is the sum that
every figure is computed at. The spread is stored in `system_config`, not
hardcoded, because it is a lender-pricing assumption that moves — change it
with `utilities/set-investor-spread.py <percent>`.

`build-cashflow-profile.py` computes no new economics: every figure is a median
over what `build-cashflow.py` already wrote, so an area answer can never
contradict the listing a user drills into.
