# Area competition — how many agents actually sell an area

`sales_grid` and `sales_hotspots` answer *where is it selling*. This answers
*who is selling it, and how few of them there are*. A hot area is only a
marketing opportunity if the volume is reachable, and that depends entirely on
whether it is held by two agents or two hundred.

Written by `src/scripts/historical/build-area-competition.py` into
`area_competition`. One document per area, property type and period.

## `agentCount` is the wrong number

"How many agents sold here" has two answers and only one is useful. The raw
distinct count is dominated by tourists. Take an area with 200 sales where one
agent did 150 and fifty agents did one each: `agentCount` is 51, which reads as
a crowded market and is the opposite of the truth.

`effectiveAgents` is the number stored for this. It is 1/HHI — the Herfindahl
numbers-equivalent — the count of **equal-sized** competitors that would produce
the concentration actually observed. For the area above it is about 1.8, which
is what a human would say reading the list: this is one agent's farm.

| Observed | `effectiveAgents` |
|---|---|
| one agent holds 100% | 1.00 |
| two agents hold 50% each | 2.00 |
| ten agents hold 10% each | 10.00 |
| one at 75%, ten at 2.5% | 1.70 |

It is the literal answer to "dominated by one agent or by ten".

### Real figures, Coachella Valley subdivisions, all years

| Subdivision | Sales | `agentCount` | `effectiveAgents` | Top agent | Level |
|---|---|---|---|---|---|
| Seven Lakes Country Club | 116 | 23 | 1.98 | 71% | `highly_concentrated` |
| Toscana Country Club | 151 | 37 | 2.25 | 66% | `highly_concentrated` |
| Trilogy | 356 | 93 | 4.15 | 47% | `moderately_concentrated` |
| Indian Ridge | 267 | 48 | 4.53 | 42% | `moderately_concentrated` |
| Sun City (Palm Desert) | 1,531 | 439 | 15.16 | 22% | `unconcentrated` |

Sun City is the valley's best-selling subdivision and is **not** a farm — 439
agents have closed there. Seven Lakes sells an eighth as much and one agent has
most of it. Those are opposite kinds of opportunity, and `sales` alone cannot
tell them apart.

## Two ranking fields, and why there is no "opportunity score"

They answer different questions and both are stored, because no single number
can carry both without weights someone invented.

### `salesPerEffectiveAgent` — how much business per competitor

`sales ÷ effectiveAgents`, identically `sales × HHI`. It is high only when an
area is **both** busy and narrowly held, which is the quantity that decides
whether the volume is worth chasing: how much business each real competitor is
actually getting.

- **Descending** — proven farms: volume exists, few people take it, so there is
  one incumbent to displace.
- **Ascending** — open ground: nobody owns it, so there is no incumbent at all,
  but winning means outworking a crowd rather than one person.

Both are opportunities, of opposite kinds, which is why no score with invented
weights is stored: the sort direction is the thesis and the caller picks it.

### `concentrationPercentile` — how unusual the concentration is

An area's rank by HHI **within its own scope and period**: the share of peer
areas whose HHI is at or below its own, with `peerAreas` as the denominator.
`0.99` means more concentrated than 99% of comparable areas. No threshold is
involved anywhere in it, which is why it is the one to filter on when the
question is "show me the narrowly held ones", not "show me the big ones".

It is relative by construction, so it moves when the population moves — the
correct behaviour for "how unusual is this" — and it is stored, so a read route
still never calculates.

`competitionLevel` is a secondary label using the **US DOJ/FTC Horizontal
Merger Guidelines** thresholds on HHI, external and citable rather than picked
here:

| `competitionLevel` | `listSide.hhi` | `effectiveAgents` |
|---|---|---|
| `highly_concentrated` | ≥ 2,500 | ≤ 4.0 |
| `moderately_concentrated` | 1,500 – 2,500 | 4.0 – 6.67 |
| `unconcentrated` | < 1,500 | > 6.67 |

**Measured on this feed those cuts are badly placed**, and the stored
distribution in `ac:meta.params.observed` is what shows it: subdivision HHI runs
p50 769, p90 1,600, p95 2,200, so only 131 of 3,177 subdivisions (4.1%) reach
`highly_concentrated`, and at zone scope p90 is 600 with 97% landing in
`unconcentrated`. Guidelines written for industries with a handful of firms do
not discriminate in a market with 240,000 licensees. Keep the label for anyone
who reads HHI; rank on the percentile.

`majorityHeld` is a fact rather than a threshold — one agent holds more than
half the area's closings — and is true for 77 of 3,177 subdivisions.

## Scopes

| `scope` | `key` | Joins |
|---|---|---|
| `zip` | `92260` | `market_history`, `area_geo` |
| `city` | `palm desert` | `market_history`, `area_geo` |
| `subdivision` | `palm desert\|pga west` | `market_history`, `area_geo` |
| `zone` | `z0.005:6753:-23637:A` | `sales_hotspots.zoneId` |

The first three are keyed exactly as `market_history` keys them, because those
are the units an agent can actually buy a mailing list for. The `zone` scope is
keyed to a hot-spot zone and denormalizes `center`, `lat`, `lng` and
`radiusMiles`, so colouring map circles by competition is one indexed query
rather than a join.

**City scope is almost always `unconcentrated`** and that is correct, not a bug: a
city is never one agent's farm. Palm Desert's `effectiveAgents` over all years
is 231. The signal lives at subdivision and zone scope.

## Shape

| Field | Meaning |
|---|---|
| `scope`, `key`, `label` | the area; `label` is the caption to render |
| `propertyType` | `A` sale, `B` lease |
| `year` | **integer** calendar year; `null` on the `allYears` companion |
| `allYears` | true on the all-time document |
| `sales` | every closing in the area-period |
| `salesWithAgent` | those with a usable, non-sentinel listing agent |
| `agentCoverage` | `salesWithAgent ÷ sales` — the honesty field |
| `listSide` | the concentration block, below |
| `buySide` | the same questions for who brought the buyers |
| `concentrationPercentile` | rank by HHI among `peerAreas` in the same scope and period |
| `peerAreas` | the denominator behind that percentile |
| `competitionLevel` | `highly_concentrated` / `moderately_concentrated` / `unconcentrated` |
| `majorityHeld` | one agent holds > 50% of the closings |
| `vintage` | median year built and new-construction share, below |
| `quality.confidence` | `good` ≥ 30 sales, `fair` ≥ 10, else `low` |
| `city`, `postalCode` | dominant city and ZIP of the sales |
| `partialYear`, `yearMonthsCovered` | read from `market_history`, not recomputed |
| `center`, `lat`, `lng`, `radiusMiles`, `zoneId` | `zone` scope only |

### `listSide`

| Field | Meaning |
|---|---|
| `salesCounted` | **the denominator of every share here** |
| `agentCount` | distinct listing agents |
| `effectiveAgents` | 1/HHI — the headline |
| `hhi` | 0–10,000 antitrust scale, for thresholds quoted that way |
| `salesPerEffectiveAgent` | the ranking field |
| `topAgentShare`, `topAgentSales` | the incumbent |
| `top3Share`, `top5Share` | the shape of the curve below them |
| `singleSaleAgents`, `singleSaleAgentShare` | share **of agents**, not of sales |
| `medianSalesPerAgent`, `maxSalesPerAgent` | the distribution's middle and top |
| `brokerageCount`, `effectiveBrokerages`, `brokerageHhi`, `topBrokerageShare` | the same at office level |
| `topAgents[]` | up to 10: `agentId`, `name`, `sales`, `share`, `volume` |
| `topBrokerages[]` | up to 5: `name`, `sales`, `share` |

`topAgents[].agentId` is `agents._id` verbatim (`<mlsSource>:<lower mlsId>`), so
the dashboard opens the agent record without a name match.

### `vintage`

An area whose sales are mostly new construction is held by whoever **built** it,
which is a different thing from a farm an agent won. That difference is a number
here, not a judgement call about brokerage names.

| Field | Meaning |
|---|---|
| `medianYearBuilt` | the area's vintage |
| `newBuildShare` | share of sales closing within `maxAgeCountedNew` years of being built |
| `newBuildSales` | the count behind it |
| `yearBuiltCoverage` | how many sales had a usable `yearBuilt` (99.9% feed-wide) |
| `declaredNewShare` | the feed's own `newConstructionYN`, where present |
| `declaredCoverage` | how many sales carried that field (87.0% feed-wide) |
| `maxAgeCountedNew` | the age cut this build used |

**Why age ≤ 1**, measured two ways that agree, with the evidence stored in
`ac:meta.params.newBuildRule`:

- Age at close falls off a cliff after 1 — 24.4% of closings at age 0, 14.9% at
  age 1, then 6.1% at age 2 and flat from there.
- Against the feed's own `newConstructionYN` (666,029 rows carrying a
  true/false), `age ≤ 1` agrees **98.9%** of the time and catches **91.8%** of
  declared-new sales. `age ≤ 0` catches only 60.6%; `age ≤ 2` drops precision
  from 85.5% to 77.5%.

`newBuildShare` is the primary figure because `yearBuilt` is 99.9% populated and
is a fact; `declaredNewShare` is a listing agent's declaration at 87% coverage.
Both are stored, neither is folded into the other, and the two disagreeing is
itself informative.

In practice this separates cases the concentration number alone cannot:

| Area | `effectiveAgents` | Top agent | `newBuildShare` | Reading |
|---|---|---|---|---|
| Seven Lakes Country Club | 1.98 | 71% | 0% | a resale farm one agent won |
| Toscana Country Club | 2.25 | 66% | 11% | mostly resale, narrowly held |
| Skyborne | 4.79 | 45% | 48% | a tract still being built out |

## Reading it

**Shares are of `salesCounted`, not of `sales`.** Dividing `topAgentSales` by
the top-level `sales` understates every share by whatever the sentinel guard
removed. `listSide` and `buySide` lose different rows, so each carries its own
denominator.

**Sentinel agents are excluded, and that is the point.** Every MLS dumps co-op
deals whose other side is not a member into one bucket member — "OUT OF AREA -
NON MEMBER" and relatives. One of these ranked first in the state with 767 deals
in a `build-agents.py` smoke run. Left in, that bucket out-sells every real
agent in an area, so the area reads as a one-agent farm. `agentCoverage` reports
what fraction of closings survived, so a thin area cannot pretend to be a
measured one.

**Teams count as one competitor.** Some rows carry a team's id with
`listAgentMemberType: "Teams/Partners"`. That is left alone: a team competes for
a listing as one unit, so counting it once is what the metric is for.

**One agent with two MLS registrations counts twice.** An `agentId` is
`<mlsSource>:<lower mlsId>`, which is `agents._id`, and that collection holds
one document per *registration* — an agent who belongs to two MLSs has two.
Their share is therefore split rather than pooled, so concentration is
understated, never overstated. That is the safe direction for a metric whose job
is to find dominance, and merging them needs the identity resolution
`build-agents.py` already does.

**Brokerages are counted by brand, not by storefront.** One brand holds many
`listOfficeMlsId` values, so offices are keyed on a normalized name instead.
Franchise identity is not collapsed: "Coldwell Banker Realty" and "Coldwell
Banker Home Source" remain different competitors, as in `build-agents.py`.

**Small samples are biased toward concentration.** HHI has a floor of 1/n, so an
area with 4 sales can never show more than 4 effective agents. `--min-sales`
(default 10) is the floor; treat anything below `quality.confidence: "fair"` as
unrankable.

**Zone figures are not comparable with city figures over the same ground.**
`latitude` is present on 65.6% of type-A closings. The zip, city and subdivision
scopes do not need it and count every closing, matching `market_history`
exactly; the `zone` scope is coordinate-bound and covers only the placeable two
thirds. `sales` is on every document so the gap is visible rather than inferred.

**2026 is a partial year.** `partialYear` and `yearMonthsCovered` say so. A raw
2026 count is not comparable to a full year.

## Querying it

Nine indexes, one per read route:

| Index | Route |
|---|---|
| `scope_year_sper` | rank areas by `salesPerEffectiveAgent` |
| `scope_year_effective` | least or most competitive first |
| `scope_year_sales` | hottest areas first |
| `scope_year_pct` | most concentrated relative to peer areas |
| `scope_year_level_sales` | concentrated areas that still have volume |
| `city_scope_year` | one city's subdivisions, and the stale sweep |
| `zone_year` | join from a `sales_hotspots` document |
| `top_agent` | where does this agent already dominate |
| `center_2dsphere` | map viewport |

```js
// the analytics tab's default table
db.area_competition.find({ scope: "subdivision", propertyType: "A", year: 2026 })
  .sort({ "listSide.salesPerEffectiveAgent": -1 }).limit(50)

// the 5% most narrowly held subdivisions, excluding tracts a builder holds
db.area_competition.find({ scope: "subdivision", propertyType: "A", year: 2026,
                           concentrationPercentile: { $gte: 0.95 },
                           "vintage.newBuildShare": { $lt: 0.25 } })
  .sort({ sales: -1 }).limit(50)

// the map layer: use $geometry, never $box (a legacy 2d operator)
db.area_competition.find({ scope: "zone", propertyType: "A", year: 2026,
  center: { $geoWithin: { $geometry: { type: "Polygon", coordinates: [[ /* … */ ]] } } } })

// every area one agent is top-10 in
db.area_competition.find({ "listSide.topAgents.agentId": "GPS:d74565", allYears: true })
```

`ac:meta` holds the build parameters, including the `competitionLevel` cut
points and the rank field. It carries no top-level `scope`, `propertyType` or
`year`, so it cannot leak into a filtered route.

## Running it

```bash
# after build-sales-hotspots.py, which the zone scope reads
python3 src/scripts/historical/build-area-competition.py --all
python3 src/scripts/historical/build-area-competition.py --cv
python3 src/scripts/historical/build-area-competition.py --all --scope city
python3 src/scripts/historical/build-area-competition.py --status
```

Concentration needs the whole per-agent distribution, not a top-N, so each
scope gets its own pass — four reads, peak memory bounded by the widest scope,
RSS printed per scope. The stale sweep is keyed on `builtAt` and narrowed to
the cities being rebuilt, so a `--cv` run cannot wipe what an `--all` run
wrote; there are no database backups.
