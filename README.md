# chatrealty-backend

Backend data pipelines for the chatrealty / jpsrealtor platform. Python jobs that
sync MLS data into MongoDB and precompute the analytics the frontend + AI chat read.

> Runs on a VPS via cron (`crontab.vps`). The Next.js frontend and the MCP/chat
> tooling live in a **separate repo** and consume the precomputed fields below.

## What's here

```
src/scripts/
├── mls/backend/unified/   MLS sync → unified_listings / unified_closed_listings / …
├── fub/                   Follow Up Boss lead sync → contacts
└── cma/                   Analytics precompute:
    ├── build-subdivision-cma.py   cmaStats (sale comps) per subdivision
    ├── build-listing-cma.py       cmaStats per active sale listing
    ├── build-rate-profiles.py     per-listing seasonal rent rateProfile
    ├── build-rent-rates.py        rentStats "going rate" per subdivision + ZIP
    └── build-cashflow.py          cashflowStats (rental investment math) per listing
```

## Analytics layers

| Concern | Producer | Output |
|---|---|---|
| Sale comps | `build-subdivision-cma.py`, `build-listing-cma.py` | `cmaStats` |
| Rental going rate | `build-rent-rates.py` | `subdivisions.rentStats`, `rent_rates` collection |
| Cash-flow scoring | `build-cashflow.py` | `unified_listings.cashflowStats` |

The rental cash-flow stack powers the chat query *"show me listings in X that
cash-flow with 20% down."* Frontend/MCP contract: **`docs/cma/RENTAL_CASHFLOW.md`**
(+ the ready-to-paste handoff in `docs/cma/RENTAL_CASHFLOW_FRONTEND_PROMPT.md`).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # fill in MongoDB + API credentials
```

Secrets live in `.env.local` (git-ignored). See `.env.example` for the required keys.

## Docs

- `docs/cma/` — CMA + rental architecture and frontend contracts
- `docs/db/` — database state, cleanup, and migration notes
