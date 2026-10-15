# Hand-off prompt for the frontend agent

Copy everything inside the fence into the frontend repo's agent. It is
self-contained — it assumes no access to the backend VPS.

```text
You are adding agent and brokerage pages to this app. The data already exists in
MongoDB, fully built and indexed. Do not scrape, re-derive, or recompute it —
read it.

## What exists

Three collections in the SAME cluster this app already connects to, database
`admin`:

  agents        240,236 docs   one per MLS registration
  brokerages     29,719 docs   one per brokerage brand
  agent_roster  158,920 docs   raw Spark member feed (source data, rarely needed)

Built by a backend job that reruns Tuesday and Friday at 3 AM. It rebuilds both
collections wholesale and then DELETES every document whose `buildId` is not the
current one.

=> THESE COLLECTIONS ARE READ-ONLY TO THIS APP. Anything you write into them
   disappears at the next rebuild. If a feature needs per-agent app state
   (claims, overrides, favourites), put it in a separate collection keyed by the
   agent's `slug` or `_id`.

## agents — document shape

`_id` is "MLS_SOURCE:agentkey", e.g. "HIGH_DESERT:hdmls-hd10850".
`slug` is the URL-safe form, e.g. "marcus-shivers-high-desert-hdmls-hd10850".

{
  _id, slug, mlsSource, agentKey,
  agentMlsId,            // "HDMLS-HD10850" — raw-case feed id
  agentMlsIdVariants[],  // EVERY casing seen; use this to query listings
  sparkKey,

  identity: {
    name, nameNormalized, firstName, lastName, viewName, nameVariants[],
    searchNames[],                  // ["marcus shivers", "shivers marcus"]
    memberType, memberTypes[], isTeam, aor, association, associations[],
    licenseNumber, email, emailsAll[], phone, phones[], photoUrl, bio, website,
    isSentinel,                     // true = MLS placeholder, NOT a real person
    personId,                       // "p:38b5ab8374511fcb" — same human across MLSs
    registrationCount,              // how many MLS records that person has
    alsoKnownAs: [ { _id, mlsSource } ],   // their other registrations
    rosterSparkId, rosterActive, rosterSource, rosterOfficeCity
  },

  brokerage: { name, slug, normalizedName },   // ABSENT for 1,393 agents
  office:    { mlsId, id, name, phone, aor, slug },
  offices:   [ { mlsId, name, phone, aor, listings } ],

  location: {
    primaryCity, primaryCitySlug, primaryPostalCode,
    cities: [ { city, slug, active, escrow, closed, offMarket, total } ],  // <=25
    cityCount, postalCodes: [ { postalCode, count } ], counties[],
    marketLabel                     // "Apple Valley, Adelanto, Boron +39"
  },

  active:    { total, sale, lease, land, income, byPropertyType, volume,
               medianListPrice, minListPrice, maxListPrice, medianDaysOnMarket,
               oldestOnMarketDate, newestOnMarketDate,
               byCity: [ { city, slug, count } ], cityCount },
  escrow:    { ...same shape... },
  offMarket: { ...same shape... },
  // ^ THESE THREE ARE OMITTED ENTIRELY when the agent has no such inventory.
  //   Presence out of 240,236 docs: active 33,507 · escrow 12,636 ·
  //   offMarket 36,122 · closed 236,469 · office 238,843.
  //   `production`, `location`, `offices` and `identity` are always present —
  //   `production` mirrors the counts and is the safe thing to render.
  //   So `agent.active.total` throws for 86% of agents. Use
  //   `agent.production.activeListings`, or optional-chain everything.

  closed: {
    windowYears: 5, windowStart, lastCloseDate,
    listSide: { deals, volume, medianClosePrice, minClosePrice, maxClosePrice,
                medianDaysOnMarket, medianSaleToListRatio, sale, lease, land, income },
    buySide:  { ...same... },
    total:    { deals, volume },
    last12mo: { deals, volume, listSide, buySide },
    byYear:   [ { year, deals, volume, listSide, buySide } ],
    byCity:   [ { city, slug, deals } ],
    statsTruncated
  },

  production: {              // THE RANKING SURFACE — precomputed, indexed
    windowDeals, windowVolume, last12moDeals, last12moVolume,
    listSideDeals, buySideDeals, primaryRole,   // "list" | "buy" | "both"
    activeListings, activeInventoryValue, inEscrowListings, avgDealPrice
  },

  activity: { status, lastActivityDate, lastActivitySource, hasLiveInventory },
             // status: "active" (38,399) | "recent" (87,733) | "dormant" (114,104)

  coverage: { sources[], listingsCounted, officeKnown, officeUnknownListings,
              rosterMatched, contactFromRoster, buySideIdentityOnly,
              confidence,          // "high" 111,401 | "low" 102,629 | "medium" 26,206
              notes[] },           // plain-English caveats — safe to show a user

  lastUpdated
}

Use `production.*` for every sort and leaderboard. Never aggregate listings at
request time to get these numbers; they are already computed and indexed.

## brokerages — document shape

`_id` IS the slug, so a route is a primary-key lookup.

{
  _id: "compass", name, slug, normalizedName,
  agentCount, agentsWithLiveInventory, officeCount,
  offices: [ { mlsId, name, phone, agents } ],         // top 50 by headcount
  mlsSources[],
  active: { listings, volume, inEscrow },
  closed: { deals, volume, last12moDeals, last12moVolume },
  cities: [ { city, slug, agents, active, closed } ],  // top 25
  cityCount, primaryCity, primaryCitySlug,
  topAgents: [ { _id, name, slug, city, activeListings,
                 closedDeals, closedVolume } ],        // top 25, sentinels excluded
  lastUpdated
}

Grain is the BRAND, not the MLS office: Compass spans 981 office ids, so keying
on office would return 1/981st of their agents. Franchise families are kept
separate on purpose — "Coldwell Banker Realty" and "Coldwell Banker Home Source"
are different brokerages. Do not merge them in the UI.

`topAgents` is a denormalized card payload. For the full roster query `agents`.

## Indexes — write queries that match these

agents:
  slug_1                  slug
  agentMlsId_1            agentMlsId
  agentMlsIdVariants_1    agentMlsIdVariants
  mlsSource_agentKey      mlsSource + agentKey
  personId_1              identity.personId
  license_1               identity.licenseNumber
  searchNames_1           identity.searchNames
  nameNormalized_1        identity.nameNormalized
  agent_text              TEXT: identity.name (w10), brokerage.name (w3), office.name (w2)
  brokerage_volume        brokerage.slug + production.windowVolume desc
  primaryCity_volume      location.primaryCitySlug + production.windowVolume desc
  cities_volume           location.cities.slug + production.windowVolume desc
  city_brokerage          location.primaryCitySlug + brokerage.slug
  status_volume           activity.status + production.windowVolume desc
  mlsSource_volume        mlsSource + production.windowVolume desc
  zips_1                  location.postalCodes.postalCode
  counties_1              location.counties
  officeMlsId_1           office.mlsId
  windowVolume_-1  windowDeals_-1  l12Volume_-1  activeTotal_-1

brokerages:
  slug_1  normalizedName_1  closedVolume_-1  activeListings_-1
  agentCount_-1  cities_volume (cities.slug + closed.volume desc)  primaryCitySlug_1

## Query patterns

// one agent  (slug_1)
Agent.findOne({ slug }).lean()

// one brokerage  (_id_)
Brokerage.findById(slug).lean()

// top agents in a city — agents who WORK the city, not just live there
Agent.find({ "location.cities.slug": "palm-desert" })
     .sort({ "production.windowVolume": -1 }).limit(25)       // cities_volume

// top agents at a brokerage
Agent.find({ "brokerage.slug": "compass" })
     .sort({ "production.windowVolume": -1 }).limit(50)       // brokerage_volume

// biggest brokerages in a city
Brokerage.find({ "cities.slug": "la-quinta" })
         .sort({ "closed.volume": -1 }).limit(25)             // cities_volume

// name search
Agent.find({ $text: { $search: q } }, { score: { $meta: "textScore" } })
     .sort({ score: { $meta: "textScore" } }).limit(20)       // agent_text

## Their actual listings

The agent doc carries counts and stats, not the properties. For the property
rows, query the listing collections with the agent's id variants:

  unified_listings          active     (list side: listAgentMlsId)
  unified_in_escrow         pending    (list side)
  unified_off_market        expired/withdrawn/cancelled (list side)
  unified_closed_listings   closed     (listAgentMlsId AND buyerAgentMlsId)

  Listing.find({ mlsSource: agent.mlsSource,
                 listAgentMlsId: { $in: agent.agentMlsIdVariants } })

`agentMlsIdVariants` exists precisely so this is an index seek with no collation
and no case-insensitive regex. Always filter on `mlsSource` too — ids are only
unique within an MLS.

PERFORMANCE WARNING — `unified_listings` is at MongoDB's hard 64-index cap, so
the agent-lookup index could NOT be created on it. That one query is a ~24
second collection scan. Every other listing collection has the index and seeks
in ~10ms. So:
  - For ACTIVE listing COUNTS and stats, read `agent.production.activeListings`
    / `agent.active.*` from the agent doc. Do not query unified_listings for
    them.
  - Only hit unified_listings for the actual active property rows, and treat it
    as a slow path: paginate, cache, or load it client-side after first paint.
  - Ask the backend owner before relying on it in a hot path; an index swap is
    pending.

## Gotchas that will produce wrong output if you ignore them

1. ONE DOC PER MLS REGISTRATION, NOT PER PERSON. Someone licensed in two MLSs
   has two documents, each with its own slice of their production. 100,639 docs
   carry `identity.personId`; `identity.alsoKnownAs` lists the siblings and
   `registrationCount` says how many. Before showing a headline count of
   "agents", or summing volume across a set, dedupe on `personId` (falling back
   to `_id` when absent). A single agent profile page should offer to merge the
   `alsoKnownAs` records rather than silently showing one third of their work.

2. SENTINELS. 516 docs are MLS placeholders, not humans — "OUT OF AREA OUT",
   "Subscriber Non", etc. One of them ranks #1 in the state by volume. ALWAYS
   add `"identity.isSentinel": { $ne: true }` to any ranking, leaderboard, or
   search. `brokerages.topAgents` already excludes them.

3. CITY RANKING IS NOT TOTAL RANKING. An agent with 1,609 deals statewide may
   have 29 in Palm Desert, losing to a local with 87 of 87. Filtering by
   `location.cities.slug` and sorting by `production.windowVolume` ranks by
   STATEWIDE production among agents who touched that city. For a true local
   leaderboard, read the per-city figure out of `location.cities[]` after the
   fetch and re-sort, or require a minimum local deal count.

4. YOU CANNOT SORT ON A MATCHED ARRAY ELEMENT. Mongo will not sort by
   `cities.$.closed`. Filter by `cities.slug`, sort by the brand-wide total,
   then fix up in JS.

5. BROKERAGE COUNTS ARE A FLOOR. 31% of active listings report an
   "Unknown Office" sentinel, which is absent data and is dropped: 1,393 agents
   have no `brokerage` subdocument at all (`coverage.officeKnown: false`) and
   21,221 more have at least one office-less listing and were attributed from
   the rest of their work. Never present `agentCount` as a brokerage's roster
   size. Guard every brokerage read: `agent.brokerage?.name`.

6. THE CLOSED WINDOW IS 5 YEARS. `closed.*` and `production.windowDeals` cover
   `closed.windowStart` forward only. Label it in the UI ("last 5 years"); do
   not call it career production.

7. BUY-SIDE-ONLY AGENTS ARE THIN, AND THERE ARE A LOT OF THEM. 127,222 docs
   (53%) have `coverage.buySideIdentityOnly: true` — the closed feed names the
   buyer's agent as a display string and gives nothing else: no licence, phone,
   office or key. They are most of the 102,629 `confidence: "low"` records. Do
   not build a profile page that looks broken for them; gate every contact block
   on the field being present, and consider excluding `low` confidence from
   public search and sitemaps.

8. CONTACT COVERAGE IS UNEVEN BY DESIGN. Across all agents: licence 41.5%,
   email 40.3%, phone 39.1%, photo 1.5%. Among `activity.status: "active"`
   agents it is far better — email 96.0%, phone 94.4%, licence 96.0% — but photo
   is only 4.1%, because just two of the eight MLSs publish member photos. Plan
   for a missing headshot as the normal case, not the exception.

9. `coverage.notes[]` is written to be shown to a human. When a profile looks
   sparse, render the notes instead of inventing an explanation.

## Suggested routes

  /agents                       search + filter (exclude sentinels)
  /agents/[slug]                profile; merge alsoKnownAs
  /brokerages                   ranked by closed.volume
  /brokerages/[slug]            offices, city footprint, topAgents
  /brokerages/[slug]/agents     full roster via brokerage_volume
  /cities/[city]/agents         local leaderboard — apply gotcha 3

Build the Mongoose models with `{ collection: "agents" }` and
`{ collection: "brokerages" }` explicitly, `versionKey: false`, and no
timestamps — the builder owns `lastUpdated`.
```
