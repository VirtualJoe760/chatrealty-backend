# chatrealty-backend

Backend data pipelines for the chatrealty / jpsrealtor platform. Python jobs that
sync MLS data into MongoDB and precompute the analytics the frontend + AI chat read.

> Runs on a VPS via cron (`crontab.vps`). The Next.js frontend and the MCP/chat
> tooling live in a **separate repo** and consume the precomputed fields below.

## What's here

```
src/scripts/
├── ingest/      live MLS feed → unified_listings / unified_closed_listings / …
├── historical/  five years of closed sales + the annual archive
├── rental/      what things rent for      (going rates, seasonal, pool premium)
├── investor/    whether it is a good buy  (cash-flow, inventory + cash-flow rollups)
├── comps/       what things sell for      (subdivision + per-listing CMA)
├── agents/      agent + brokerage database
├── search/      chat autocomplete index
└── utilities/   hand-run tools and spent one-shots
```

One folder per domain, one level deep. `docs/scripts.md` is the index of every
script, what it writes and when it runs.

## Analytics layers

| Concern | Producer | Output |
|---|---|---|
| Sale comps | `build-subdivision-cma.py`, `build-listing-cma.py` | `cmaStats` |
| Rental going rate | `build-going-rates.py` | `subdivisions.rentStats`, `rent_rates` collection |
| Seasonal rates (asking) | `build-seasonal-rates.py` | `rentStats.seasonal` |
| Seasonal rates (achieved) | `build-closed-seasonal-rates.py` | `rentStats.closedSeasonal` |
| Private-pool rent lift | `build-pool-premium.py` | `rentStats.poolPremium` |
| Cash-flow scoring | `build-cashflow.py` | `unified_listings.cashflowStats` |
| Inventory mix | `build-inventory-profile.py` | `inventoryProfile` |
| **Cash-flow rollup** | `build-cashflow-profile.py` | **`cashflowProfile`** — the one-findOne answer |
| **Agents & brokerages** | `build-agents.py` | **`agents`** (240,236), **`brokerages`** (29,703) |

## Building AI tools against this data

**If you are writing an MCP tool or a frontend reader, start here:**

- **`docs/strategies/tool-development.md`** — the precompute-then-serve strategy
  and its non-negotiable rules. The model must never calculate; it looks up a
  figure a cron job already computed and says where it came from.
- **`docs/tools/`** — the data contract for every precomputed block: producer,
  lookup key, exact shape, real coverage counts, refresh cadence, and reading
  rules. Start at `docs/tools/README.md`.

The rental cash-flow stack powers the chat query *"show me listings in X that
cash-flow with 20% down."* Deeper architecture notes live in
`docs/cma/RENTAL_CASHFLOW.md`; `docs/cma/RENTAL_CASHFLOW_FRONTEND_PROMPT.md` is
an earlier single-tool handoff, superseded for new work by `docs/tools/`.

## MCP server (runs here, unlike the rest)

The agent database ships with its own MCP server — the one exception to the
"tools live in the frontend repo" rule, because it is also what Claude Code on
this VPS uses:

```bash
.venv/bin/python src/mcp/agents_server.py                       # stdio
.venv/bin/python src/mcp/agents_server.py \
    --transport streamable-http --port 8787                     # hosted
```

7 tools: `search_agents`, `get_agent`, `get_agent_listings`, `rank_agents`,
`list_brokerages`, `get_brokerage`, `agent_database_stats`. Registered for
Claude Code in `.mcp.json`. Contract: `docs/tools/agents.md`.

Whether the rest of the MCP layer should move here too — and whether the
database should — is worked through in `docs/db/vps-migration-plan.md`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # fill in MongoDB + API credentials
```

Secrets live in `.env.local` (git-ignored). See `.env.example` for the required keys.

## Docs

- `docs/tools/` — **data contracts for AI/MCP tool authors** (start here)
- `docs/strategies/` — how we build tools, and why the data is shaped this way
- `docs/cma/` — CMA + rental architecture, algorithm detail, worked examples
- `docs/db/` — database state, cleanup, and migration notes
