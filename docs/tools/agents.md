# `agents` + `brokerages` — the agent database

**Producers:** `src/scripts/agents/sync-agents-roster.py` (contact detail),
then `src/scripts/agents/build-agents.py` (everything else)
**Collections:** `agents` (one doc per MLS registration), `brokerages` (one doc
per brokerage brand), `agent_roster` (one doc per Spark member account)
**Served by:** `src/mcp/agents_server.py` — 7 MCP tools, stdio or
streamable-http
**Shape:** mostly **tool shape 2 and 3** (list / rank) from
`docs/strategies/tool-development.md`, with one shape-1 lookup (`get_agent`).
Everything is precomputed; the tools never calculate.

> Read `docs/strategies/tool-development.md` and
> [how-to-call.md](how-to-call.md) first. The rules there about tight
> projections, deterministic sorts, stating your cap, and superlatives landing
> on your worst data all apply here — the last one especially. See
> §"Placeholder records" below; it is the single thing most likely to make an
> agent answer embarrassing.

---

## Why this exists

There is **no agent index on any listing collection.** Verified 2026-10-14:
`unified_listings` has 64 indexes, `unified_closed_listings` 46,
`unified_in_escrow` and `unified_off_market` 11 each, and not one of those 132
indexes mentions an agent or office field. Every "who sold this", "how many
listings does she have", "top agents in Palm Desert" question was therefore a
full collection scan over 3.9 GB of closed listings.

These two collections turn all of that into an indexed seek.

---

## The identity key, and why it is not what you'd expect

**`_id` = `"<mlsSource>:<lower(trim(listAgentMlsId))>"`** — e.g. `"GPS:d22668"`.

The obvious key would be `listAgentKey` / `listAgentId`, the Spark universal
26-digit member key. **Do not use it.** Measured by sampling 20,000 docs per
collection on 2026-10-14:

| Field | `unified_listings` | `unified_closed_listings` |
|---|---:|---:|
| `listAgentKey` | 32.4% | 22.0% |
| **`listAgentMlsId`** | **100.0%** | **100.0%** |
| `listAgentName` | 100.0% | 99.9% |
| `buyerAgentMlsId` | 0.0% | **99.9%** |
| `listAgentPreferredPhone` | 6.8% | 4.1% |
| `listOfficeName` | 100.0% | 100.0% |

So the grain is the **MLS registration**. It is lowercased because the feed is
inconsistent about case (`"D78163"` alongside `"d84657"`), and namespaced by
`mlsSource` because `listAgentMlsId` is only unique within an MLS — GPS uses
bare `D22668` while CRMLS uses `MRM-SC01956562`.

`sparkKey` carries `listAgentKey` where it exists, so you can still join to
`mlsagents` or the Spark feed.

### One person, several records

A person registered in both CRMLS and GPS is **two documents**, and each one's
production covers that registration only. Merging on name would fuse every
"Maria Garcia" in Southern California, so instead the builder clusters
conservatively and tells you about it:

| Field | Meaning |
|---|---|
| `identity.personId` | Present only when two or more registrations were *proved* to be one person |
| `identity.alsoKnownAs` | `[{_id, mlsSource}]` — the sibling registrations |
| `identity.registrationCount` | 1 when unlinked |

Clustering fires on a shared licence number, or an exact normalized name plus
either a shared 10-digit phone or the same brokerage brand. A signature matching
more than 6 registrations is discarded rather than trusted — that is the
fingerprint of a common name, not of one busy agent.

**Reading rule:** when a user asks for "her total career volume" and
`alsoKnownAs` is non-empty, say the figure covers one MLS registration, and
offer to add the others. Do not silently sum them — a co-brokered deal can
appear in both.

---

## Placeholder records — read this before ranking anything

An MLS files every co-op deal where the other side was not a member against a
single placeholder member id. Those ids therefore accumulate more production
than any real agent. A smoke build on a 25,000-doc sample ranked this first in
the state:

```
OUT OF AREA OUT  @  OUT OF AREA - NON MEMBER   767 deals   $721,133,484
```

Extrapolated over the full collection that is tens of thousands of deals, from
hundreds of unrelated agents. **`identity.isSentinel: true` marks these.**

- `rank_agents` excludes them. Always.
- `search_agents` excludes them unless `include_placeholders=true`.
- `brokerages.topAgents` excludes them.
- `coverage.confidence` is forced to `"low"` and `coverage.notes` explains why.

Measured: 40 of 33,338 agents in the smoke build (0.12%), every one a genuine
placeholder, and no real agent flagged. The matcher is phrase-based so that
"Noni Area", "Area Pro Realty", "Boardman Group" and "Montana Stateman" survive.

---

## The closed window, and the `closeDate` trap

`closed.*` covers a **rolling 5 years** (`closed.windowYears`,
`closed.windowStart`). That is not an arbitrary choice: the database enforces it.
`unified_closed_listings` carries a real TTL index — `closeDate_ttl_5years`,
`expireAfterSeconds: 157_680_000` — so older closed listings are **deleted**.
Career history beyond 5 years cannot be recovered from this data, and an agent's
`windowDeals` is not their lifetime count.

`closeDate` is **mixed BSON types** (measured 2026-10-14):

| Type | Docs | Range |
|---|---:|---|
| `date` | 1,028,003 | 2021-10-16 → (35 corrupt, worst year 5015) |
| `string` | 84,903 | 2020-08-28 → 2026-10-12 |
| missing | 4 | — |

Two consequences, and they bite in opposite directions:

- `{closeDate: {$gte: <datetime>}}` silently drops **all 84,903 string rows**,
  because BSON sorts strings before dates. They run to 2026-10-12 — current
  data, not junk.
- `{closeDate: {$gte: "2021-10-14"}}` drops **all 1,028,003 date rows**.
- The TTL can only expire date-typed values, so the strings are exempt and
  reach back past the 5-year horizon.

This builder filters on a converted value (`$convert ... to date`, `onError:
null`) and clamps the future at `now + 7 days` to drop the 35 corrupt rows.
**If you write any other closed-listing reader, do the same.** The existing CMA
builders do not — see `docs/db/vps-migration-plan.md` §5 for the specific lines
and the recommended one-off normalization.

---

## Document shape — `agents`

Trimmed; `get_agent` returns all of it.

```js
{
  _id: "GPS:d22668",
  slug: "susan-harvey-gps-d22668",       // stable, safe in a URL
  agentMlsId: "D22668",
  agentMlsIdVariants: ["D22668","d22668"],  // every casing seen; see below
  agentKey: "d22668",
  mlsSource: "GPS",
  sparkKey: "2019...",                   // = listAgentKey, when present

  identity: {
    name, firstName, lastName, viewName, marketingName, nameVariants[],
    nameNormalized, searchNames[],       // indexed; what search_agents matches
    slug, phone, phones[], memberType, memberTypes[], isTeam,
    aor, association,
    licenseNumber, email, emailsAll[], photoUrl, bio,   // from mlsagents only
    rosterSparkId, rosterActive, accountId,
    isSentinel,                          // NOT A REAL AGENT — see above
    personId, alsoKnownAs[], registrationCount
  },

  office:    { id, mlsId, name, phone, aor, slug },   // the primary office
  brokerage: { name, slug, normalizedName },          // the brand
  offices:   [ { mlsId, id, name, phone, aor, listings } ],   // history, ≤10

  location: {
    primaryCity, primaryCitySlug, primaryPostalCode,
    counties[], cityCount, marketLabel,              // "Palm Desert, Indio +3"
    cities:      [ { city, slug, active, escrow, closed, offMarket, total } ],
    postalCodes: [ { postalCode, count } ]
  },

  active: {       // and `escrow`, `offMarket` — same shape
    total, sale, lease, land, income, byPropertyType{},
    volume, medianListPrice, minListPrice, maxListPrice,
    medianDaysOnMarket, oldestOnMarketDate, newestOnMarketDate,
    byCity[], cityCount
  },

  closed: {
    windowYears, windowStart,
    listSide: { deals, volume, medianClosePrice, minClosePrice, maxClosePrice,
                medianDaysOnMarket, medianSaleToListRatio,
                sale, lease, land, income },
    buySide:  { ...same... },
    total:    { deals, volume },
    last12mo: { deals, volume, listSide, buySide },
    byYear:   [ { year, deals, volume, listSide, buySide } ],
    byCity[], cityCount, lastCloseDate,
    dealRowsConsidered, dealRowsTotal, statsTruncated, truncationNote
  },

  production: {
    windowDeals, windowVolume, last12moDeals, last12moVolume,
    listSideDeals, buySideDeals, primaryRole,     // "listing" | "buyer" | "both"
    activeListings, activeInventoryValue, inEscrowListings, avgDealPrice
  },

  activity: { status, lastActivityDate, lastActivitySource, hasLiveInventory },
             // status: "active" (live inventory) | "recent" (≤18mo) | "dormant"

  coverage: {
    sources[], listingsCounted, officeKnown, officeUnknownListings,
    rosterMatched, buySideIdentityOnly, confidence, notes[]
  },
  lastUpdated
}
```

### `agentMlsIdVariants` is how you get their listings

`listAgentMlsId` is stored raw-case in the listing collections, and `_id` is
lowercased. Rather than a case-insensitive regex (which cannot use an index),
every casing observed for an agent is kept in `agentMlsIdVariants`, so a lookup
is a plain indexed `$in`:

```js
db.unified_closed_listings.find({
  mlsSource: agent.mlsSource,
  listAgentMlsId: { $in: agent.agentMlsIdVariants }
})
```

This is backed by `agentlookup_listAgent` / `agentlookup_buyerAgent`, created by
`build-agents.py --listing-indexes`. **Without those indexes this is a
collection scan** — confirm they exist before shipping a listings tool.

---

## Document shape — `brokerages`

`_id` = brand slug. The grain is the **brand, not the MLS office**, because one
brand fragments across many offices — on active listings, Compass has 153
distinct `listOfficeMlsId`s, Coldwell Banker Realty 101, Century 21 Masters 53.
Asking "who works at Compass" by office id returns 1/153rd of them.

Franchise families are deliberately **not** collapsed: only legal-suffix and
punctuation noise is normalized, so `coldwell-banker-realty` and
`coldwell-banker-home-source` stay separate — they are different brokerages.

```js
{
  _id: "compass", name, slug, normalizedName,
  agentCount, agentsWithLiveInventory, officeCount,
  offices: [ { mlsId, name, phone, agents } ],        // ≤50
  mlsSources[],
  active: { listings, volume, inEscrow },
  closed: { deals, volume, last12moDeals, last12moVolume },
  cities: [ { city, slug, agents, active, closed } ], // ≤25
  cityCount, primaryCity, primaryCitySlug,
  topAgents: [ { _id, name, slug, city, activeListings,
                 closedDeals, closedVolume } ],       // ≤25, sentinels excluded
  lastUpdated
}
```

---

## Coverage and its limits

Report these when a user asks how complete the data is; `agent_database_stats`
returns them with live counts.

- **Brokerage is unknown for a lot of agents.** `listOfficeName` is the literal
  string `"Unknown Office"` on **26,735 of 86,124 active listings (31%)**, under
  one office id, across CRMLS/CLAW/SOUTHLAND/BRIDGE/HIGH_DESERT. It means absent
  data, not a real brokerage, so it never becomes a `brokerages` doc and those
  agents have no `brokerage` field. Brokerage totals are a **floor**.
  Those listings do still carry real agent names and ids, so the agent is
  attributable even when their office is not.
- **Buyer-side records are identity-thin.** The feed has no
  `buyerAgentKey`, no first/last name, no phone and no office id — only a
  display name. Agents known *only* from the buyer side get
  `coverage.buySideIdentityOnly: true` and low confidence.
- **Licence / email / phone come from the roster, not from listings.** The feed
  itself is nearly useless for contact detail — `listAgentPreferredPhone` is
  6.8% populated on active listings and 4.1% on closed, and there is no email
  field at all. Measured after the roster join, 2026-10-14:

  | | All 240,236 agents | The 38,399 **active** ones |
  |---|---:|---:|
  | Email | 96,896 (40.3%) | **96.0%** |
  | Phone | 93,917 (39.1%) | **94.4%** |
  | Licence | 99,682 (41.5%) | **96.0%** |
  | Photo | 3,519 (1.5%) | 4.1% |

  The gap between the two columns is the point: coverage is ~96% for agents
  who are currently working and thin for the long tail of dormant
  buyer-side-only records, which is the right shape. `coverage.rosterMatched`
  and `coverage.contactFromRoster` say whether a given agent got any.
- **`production` counts both sides.** An agent who mostly represents buyers
  ranks on that work too. `production.primaryRole` distinguishes them.
- **A city match means "has listed or closed there"**, not "is based there", so
  one agent legitimately appears in several cities' rankings. This matters more
  than it sounds: ranking Palm Desert by volume puts Todd Myatt (Ontario, 1,609
  deals career-wide) first on the strength of **29** deals there, ahead of
  Lorna Ball whose 87 deals are *all* in Palm Desert. Two mitigations, both
  precomputed, neither computed at call time:
  - every row carries **`inThisCity`** — the agent's `location.cities[]` entry
    for the filtered city. Quote that, not the career total, when answering a
    local question.
  - **`min_city_deals`** filters on it (`min_city_deals=25` on Palm Desert
    takes 121 agents and puts actual local leaders at the top).

  True per-city *ranking* would need a precomputed per-city leaderboard; sorting
  on a nested array element cannot use an index. Until then, filter and quote
  `inThisCity`.
- **Stats can be truncated.** Records with more than 15,000 closed-deal rows in
  the window have their breakdowns computed from a capped sample;
  `closed.statsTruncated` and `closed.truncationNote` say so. In practice this
  only ever hits placeholder ids.

---

## The roster layer — `agent_roster`

`agents` is derived from listings, which carry production and location but
almost no way to contact anybody. `sync-agents-roster.py` fills that in from
Spark's `/v1/accounts` endpoint, which serves the member roster for **all eight**
MLSs. A full sync on 2026-10-14 took 44.3 minutes and wrote **158,692** members,
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

## Rebuilding

```bash
# roster first — build-agents joins it for every agent's contact detail
.venv/bin/python src/scripts/agents/sync-agents-roster.py --all

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
