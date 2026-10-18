# AGENTS.md

Operating rules for agents working in this repository, and the map of what the
backend is.

## Responses

**Any response over ~2000 characters MUST end with a `## TL;DR` section.**

Do not shorten the response to dodge this. Long is fine — detail belongs in the
body. The TL;DR is what gets read first, so it has to stand alone.

The TL;DR is 3-5 bullets, max ~600 characters total, and covers only:

1. **What changed** — what is actually different now, not what I explored.
2. **What's broken or blocked** — the real blocker, named once.
3. **What I need from you** — the specific next action, or "nothing".

Rules for the TL;DR:

- If there is no action needed, say **"Nothing needed from you."** explicitly.
- Never repeat a caveat, cost, or apology that already appears in the body.
  Say it once, in the body, and leave it out of the TL;DR.
- Name blockers once. If I have raised the same blocker before and it is still
  blocking, that is a signal my approach is wrong — say so and propose a
  different approach instead of repeating the ask.

## Documentation

**Docs match the code, always.** Update every doc you read in the same change. A
decision made, a question resolved or a spec found wrong is fixed in the same
commit, not after. If a doc contradicts the code, the doc is wrong: fix it in
the same session. Docs describe the system as it is now, with no dates, change
logs, run reports or notes on who asked for what. No markdown file goes over 300
lines, because every doc costs tokens each time an agent opens it. A catalogue
that code parses is data, not a doc, and moves into code once it outgrows that
limit.

## Approvals

When a tool call is blocked, ask for approval in the conversation and then run
it. Never suggest opening a second terminal or SSH session to run something an
agent could run with a yes.

Work already authorized — the operating rules at the top of a build plan, a
stated preference, a scope agreed earlier in the session — is standing approval.
Do not hand the task back to re-ask for it. Reserve asking for genuinely new,
destructive or outward-facing actions that earlier instructions did not cover.

Conversational approval does not change the harness classifier, so the same
command may still need a Bash permission rule or a `/config` change. Say that
once; do not redirect to a terminal.

## Job cadence

Listing-level precompute that fans out over the whole active set runs **twice a
week**, not nightly. Comp and rent data drift slowly, and a sub-hour budget
would cost comp quality — a smaller candidate pool and dropped scoring factors —
for no product benefit. A 4-12 hour runtime is acceptable for these jobs.

Smaller rollups that finish in under an hour stay daily. `build-subdivision-cma.py`
(1,426 docs) is daily; `build-listing-cma.py` (tens of thousands of listings) is
weekly.

Never trade algorithm fidelity for a tighter slot.

## Backend architecture

Everything here is the **Brain** from the site architecture rule: the layer that
computes value, lives server-side, and improves for every client site without a
client release. Nothing in this repo ships to a customer.

| Layer | Lives | Update path | Examples |
|---|---|---|---|
| Substrate | MongoDB `admin` (DigitalOcean) | written only by ingest + precompute | `unified_listings`, `unified_closed_listings`, `subdivisions`, `cities` |
| Ingest | `src/scripts/ingest/`, `src/scripts/historical/` | cron | `run-pipeline.py`, `fetch-photos.py`, `sync.py` |
| Precompute | `src/scripts/rental/`, `investor/`, `comps/`, `agents/`, `search/` | cron, dependency-ordered chains | `build-cashflow.py`, `build-going-rates.py`, `build-agents.py` |
| Read surface | `src/mcp/`, platform API | code deploy | `agents_server.py` |

**The invariant: the read surface never calculates.** Every number a tool
returns is a stored field. A tool answers with one `findOne` by slug or key, or
one `find` with a filter and sort over precomputed fields. If a question needs
arithmetic at request time, the arithmetic belongs in a precompute stage and the
answer belongs in a field.

### Substrate

One database, `admin`, on a single cluster. The SRV alias
`jpsrealtor-mongodb-911080c1` resolves to node `jpsrealtor-mongodb-3455c31a` —
these are one cluster, not two. pymongo prints the node name in errors, which
makes it look like two.

Canonical listing collections are the `unified_*` family: `unified_listings`,
`unified_closed_listings`, `unified_in_escrow`, `unified_off_market`. Geography
reference data is `cities`, `counties`, `regions`, `subdivisions`. Derived
aggregates get their own collections (`rent_rates`) or ride as fields on the
documents they describe (`cashflowStats`, `cashflowProfile`, `cmaStats`).

See `docs/db/` for the operational constraints that the code does not record.

### Precompute

Each stage reads what earlier stages wrote and writes exactly one field or
collection, so a stage can be rerun alone and the chain has an order. The
dependency order is encoded in `crontab.vps`, which is the schedule of record;
read it before adding a stage.

`docs/scripts.md` is the index of every script, what it writes and when it
runs — including where the mortgage and cash-flow math lives. Start there.

One folder per domain, one level deep, so the folder name is the category:

```
src/scripts/
├── rental/      6  what things rent for
├── investor/    3  whether it is a good buy
├── comps/       2  what things sell for
├── ingest/      8  live MLS feed -> unified_*
├── historical/  4  five years of closed sales
├── agents/      2  agent + brokerage database
├── search/      1  chat autocomplete index
└── utilities/  11  hand-run tools and spent one-shots
```

Every script sits exactly one level under `src/scripts/`, which is what lets
each one find `.env.local` at `parents[3]`. Keep it that way: a script nested
deeper silently fails to load the environment. Each folder carries a README, and
the folder name already says the domain, so a filename should not repeat it.

A rollup is a median over stored per-item figures, never a fresh computation, so
an area answer can never disagree with the item a user drills into.

### Read surface

`src/mcp/agents_server.py` serves the agent and brokerage data. Listing, CMA and
cash-flow data is served by the platform API, which is not in this repo.

Tool conventions — tight projections, deterministic sorts, stating your cap,
superlatives landing on your worst data — are in
`docs/strategies/tool-development.md` and `docs/tools/how-to-call.md`. Read both
before adding a tool.
