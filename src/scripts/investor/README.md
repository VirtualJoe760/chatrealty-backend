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
amortized payment, its inverse, and the break-even down-payment solve. There is
no separate mortgage calculator. `docs/scripts.md` lists those functions and
`docs/cma/RENTAL_CASHFLOW.md` has the architecture.

`build-cashflow-profile.py` computes no new economics: every figure is a median
over what `build-cashflow.py` already wrote, so an area answer can never
contradict the listing a user drills into.
