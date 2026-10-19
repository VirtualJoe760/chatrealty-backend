# `agents` + `brokerages` — the agent database

**Producers:** `agents/sync-roster.py` (contact detail), then
`agents/build-agents.py` (everything else)
**Collections:** `agents` (per MLS registration), `brokerages` (per brand),
`agent_roster` (per Spark member account)
**Served by:** `src/mcp/agents_server.py` — 7 MCP tools
**Shape:** mostly **tool shape 2 and 3** (list / rank), with one shape-1 lookup
(`get_agent`). Everything is precomputed; the tools never calculate.

> Read `docs/strategies/tool-development.md` and
> [how-to-call.md](how-to-call.md) first — especially "superlatives land on your
> worst data". See §"Placeholder records"; it is the single thing most likely to
> make an agent answer embarrassing.

---

## Why this exists

There is **no agent index on any listing collection** — not one of the 132
indexes across the four `unified_*` collections mentions an agent or office
field. Every "who sold this", "how many listings does she have", "top agents in
Palm Desert" question was a full scan over 3.9 GB of closed listings. These two
collections turn that into an indexed seek.

---

## The identity key, and why it is not what you'd expect

**`_id` = `"<mlsSource>:<lower(trim(listAgentMlsId))>"`** — e.g. `"GPS:d22668"`.

The obvious key would be `listAgentKey` / `listAgentId`, the Spark universal
26-digit member key. **Do not use it.** Measured by sampling 20,000 docs per
collection:

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

## The closed window

`closed.*` covers a **rolling 5 years** (`closed.windowYears`,
`closed.windowStart`). The database enforces it: `unified_closed_listings`
carries a real TTL index — `closeDate_ttl_5years`, `expireAfterSeconds:
157_680_000` — so older closed listings are deleted. Career history beyond 5
years cannot be recovered, and `windowDeals` is not a lifetime count.

`closeDate` is **BSON date throughout** — query it with a `datetime`, never an
ISO string, which matches nothing. It was once mixed date + string, which
silently halved any naive window filter; `src/scripts/ingest/closedate.py` now
normalizes it on every write. This builder still filters on a `$convert`-ed
value, which is defensive rather than required. History in
[../db/vps-migration-plan.md](../db/vps-migration-plan.md) §5.

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
  field at all. Measured after the roster join:

  | | All 240,236 agents | The 38,399 **active** ones |
  |---|---:|---:|
  | Email | 96,896 (40.3%) | **96.0%** |
  | Phone | 93,917 (39.1%) | **94.4%** |
  | Licence | 99,682 (41.5%) | **96.0%** |
  | Photo | 3,519 (1.5%) | 4.1% |

  The gap between the columns is the point: ~96% for agents currently working,
  thin for the dormant buyer-side-only tail. `coverage.rosterMatched` and
  `coverage.contactFromRoster` say whether a given agent got any.
- **`production` counts both sides**, so an agent who mostly represents buyers
  ranks on that work too; `production.primaryRole` distinguishes them.
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

  True per-city *ranking* needs a precomputed leaderboard — sorting on a nested
  array element cannot use an index. Until then, filter and quote `inThisCity`.
- **Stats can be truncated.** Over 15,000 closed-deal rows in the window and
  breakdowns come from a capped sample (`closed.statsTruncated`). In practice
  this only hits placeholder ids.

---
---

## See also

- [agents-roster.md](agents-roster.md) — the `agent_roster` layer and its join key
- [agents-mcp.md](agents-mcp.md) — the 7 MCP tools, and rebuilding
- [frontend-agent-prompt.md](frontend-agent-prompt.md) — copy-paste spec for a
  frontend agent reading these collections
