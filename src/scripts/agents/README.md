# Agent scripts

The agent and brokerage database.

```
agents/
├── sync-roster.py    Spark member accounts -> agent_roster
└── build-agents.py   -> agents, brokerages
```

Chained in that order: `agents` is built from listing data, which carries
production but not contact detail, so the roster supplies the rest.

Served by `src/mcp/agents_server.py`. See `docs/tools/agents.md`.
