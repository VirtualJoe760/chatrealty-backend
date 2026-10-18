# `agent_roster` — the contact-detail layer

Produced by `src/scripts/agents/sync-roster.py`. Joined into `agents` by
`build-agents.py`; see [agents.md](agents.md) for the collections it feeds.

---

## The roster layer — `agent_roster`

`agents` is derived from listings, which carry production and location but
almost no way to contact anybody. `sync-roster.py` fills that in from
Spark's `/v1/accounts` endpoint, which serves the member roster for **all eight**
MLSs. A full sync takes ~45 minutes and writes ~158,900 members,
of which 158,689 carry a join key, 151,888 an email (95.7%) and 149,631 a
licence number (94.3%). Per-MLS, sampled 400 each:

| MLS | Members | ShortId | Licence | Email | Phone | Photo |
|---|---:|---:|---:|---:|---:|---:|
| CRMLS | 114,820 | 99.8% | 94.2% | 95.2% | 84.8% | 1.0% |
| CLAW | 16,513 | 100% | 95.0% | 99.8% | 99.0% | 0% |
| SOUTHLAND | 9,432 | 100% | 99.0% | 100% | 97.8% | 0% |
| GPS | 4,366 | 100% | 92.0% | 97.8% | 68.8% | 41.8% |
| ITECH | 4,156 | 100% | 90.5% | 97.8% | 80.8% | 0% |
| BRIDGE | 4,114 | 100% | 93.0% | 100% | 94.8% | 0% |
| HIGH_DESERT | 3,468 | 100% | 89.5% | 100% | 48.2% | 0% |
| CONEJO_SIMI_MOORPARK | 1,806 | 100% | 96.0% | 99.8% | 78.0% | 48.8% |

**Photos are a desert-market feature only.** Only GPS (41.8%) and Conejo (48.8%)
syndicate member images; everywhere else it is 0%. Do not promise an agent
photo outside those two.

### The join key is ShortId, not the Spark id

```js
agent_roster.agentRef  ==  agents._id      // "<mlsSource>:<lower(ShortId)>"
```

Spark's `ShortId` (and its twin `OriginatingSystemMlsId`) is the member's
per-MLS short code — `"d86130"`, `"MRM-CCBE-206531882"` — which is exactly what
listings carry in `listAgentMlsId`. **Do not join on the 26-digit Spark `Id`.**
That maps to `agents.sparkKey`, which is 22-35% populated, and on a
3,199-member sample it matched nothing at all for CRMLS, HIGH_DESERT, BRIDGE or
ITECH, while ShortId matched for all eight. `build-agents.py` tries `agentRef`
first, then `sparkKey`, then exact name plus a shared phone, and records which
one hit in `identity.rosterSource`.

Roughly 57% of roster members match an agent record. The rest are licensed
members with no listing or closing inside the 5-year window — correctly absent
from `agents`, but kept in `agent_roster` so a lookup by name or licence can
still find them.

Licence numbers also make person-clustering better, because a DRE number is the
one signature that does not depend on matching names: linked registrations went
from 95,759 to 100,639 once the roster was joined.

**A roster refresh needs a full `build-agents.py --all`.** The contact join
happens in compose_pass, which reads staging subdocuments that the previous
build stripped, so `--skip-live --skip-closed` would compose nothing. The script
refuses that combination rather than silently no-opping. In `crontab.vps` the
two run chained with `&&` for this reason.

`mlsagents` (4,720 docs, desert only) is the legacy roster. Nothing in this repo
writes it, so it is treated as read-only and used only as a fallback that never
overwrites `agent_roster`.

