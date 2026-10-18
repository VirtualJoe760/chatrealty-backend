# Serving and rebuilding the agent database

The read surface over `agents` / `brokerages` ([agents.md](agents.md)) and the
jobs that build them.

---

## MCP tools

| Tool | Shape | Use it for |
|---|---|---|
| `search_agents` | list | a named agent, or "agents in X" with filters |
| `get_agent` | look up | everything on one agent; accepts `_id`, slug, MLS id or name |
| `get_agent_listings` | list | the actual properties: `active`, `escrow`, `closed`, `closed_buyside`, `offmarket` |
| `rank_agents` | rank | "top agents in Palm Desert", "biggest producers at Compass" — pass `min_city_deals` for local leaders |
| `list_brokerages` | rank | "biggest brokerages in La Quinta" |
| `get_brokerage` | look up | one brokerage in full, with its top agents |
| `agent_database_stats` | look up | coverage + freshness, before claiming completeness |

Ambiguity is never resolved by guessing: when a reference matches several
records, the tool returns `{found: false, reason: "ambiguous", candidates: [...]}`
and the model must pick. Absent results explain themselves rather than
returning an empty list.

### Running it

```bash
# stdio — registered in .mcp.json, which is what Claude Code on the VPS uses
.venv/bin/python src/mcp/agents_server.py

# HTTP, for a hosted deployment
.venv/bin/python src/mcp/agents_server.py --transport streamable-http --port 8787
```

---


---

## Rebuilding

```bash
# roster first — build-agents joins it for every agent's contact detail
.venv/bin/python src/scripts/agents/sync-roster.py --all

# full rebuild, ~5y window
.venv/bin/python src/scripts/agents/build-agents.py --all --verbose

# first run on a new cluster: also index the listing collections
.venv/bin/python src/scripts/agents/build-agents.py --all --listing-indexes

# fast correctness check, caps every scan
.venv/bin/python src/scripts/agents/build-agents.py --smoke --verbose
```

The build is five streamed passes (active, escrow, off-market, closed, then
compose) followed by clustering and the brokerage rollup. Each pass upserts its
own subdocument, so an interrupted run is resumed by re-running it. Agents that
fall out of the window are swept by `buildId` at the end of an `--all` run.

Memory stays flat — the grouping happens server-side on the managed cluster and
only grouped rows cross the wire, which matters because this VPS has 3 GB of RAM
(see `docs/db/vps-migration-plan.md`).

**Cadence:** twice weekly, consistent with the other data-refresh jobs — see
`crontab.vps`, where the two scripts are chained with `&&` so the builder never
reads a half-synced roster. Agent production moves slowly and fidelity beats
cadence.
