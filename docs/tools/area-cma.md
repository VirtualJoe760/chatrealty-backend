# Area CMA — comps for a drawn parcel

Three CMAs now exist and they answer different questions:

| Scope | Needs | Producer |
|---|---|---|
| listing | a subject property | `comps/build-listing-cma.py` |
| subdivision | a name | `comps/build-subdivision-cma.py` |
| **parcel** | **two coordinates** | `comps/area-cma.py` |

A parcel drawn on a map has no subject and no name, so it cannot be scored and
cannot be looked up. What it can be asked is the only question a rectangle
answers: what sold inside it, what is for sale inside it, and how those compare.

The heat map calls it with one `sales_grid` cell's `bbox`. The live map calls it
with whatever the user drew. Same code path — a rectangle is a polygon with four
corners.

## The two pieces

`src/scripts/comps/build-cma-comps.py` builds `cma_comps`: one document per
comparable, closed sale and active listing in the same collection, every figure
precomputed and every row placed.

`src/scripts/comps/area-cma.py` is the read side. It contains no field parsing
at all — one `find` and medians over stored per-item figures. If a reader ever
has to parse a field, the parse belongs in the builder.

```bash
# a heat-map cell, by its sales_grid _id
src/scripts/comps/area-cma.py --cell g0.005:6750:-23261:A

# an arbitrary rectangle
src/scripts/comps/area-cma.py --bbox -116.33,33.70,-116.29,33.74

# an arbitrary drawn ring, closed automatically
src/scripts/comps/area-cma.py --polygon -116.33,33.70 -116.29,33.70 -116.30,33.74

# machine-readable, 12-month closed window by default
src/scripts/comps/area-cma.py --cell g0.005:6750:-23261:A --months 24 --json
```

## Why a separate collection

`cmaStats` hangs off a listing because it describes that listing. A parcel query
describes a *set* spanning two collections, so a subdocument would force the
reader to query `unified_listings` and `unified_closed_listings` separately and
merge them. One collection with a `status` discriminator makes it a single
query. It is also rebuildable and drops without touching the feed, which matters
on a single-node cluster with no backups that a dev-PC sync shares.

## Why `$geoWithin` works here and not on the feed

Two source indexes exist and neither is both complete and fast.

`unified_closed_listings.coordinates` is 2dsphere indexed but present on only
**26.5%** of Coachella Valley type-A closings, while `latitude`/`longitude` are
on **100%**. A geo query against it silently under-returns: measured on a 2-mile
box, 469 rows by geo against 532 by scalar. Nothing errors — the map just shows
fewer sales than happened.

The scalar pair is complete, but a compound B-tree range-scans only its leading
key. Against `latitude_1_longitude_1_closeDate_1_propertyType_1` the same 2-mile
box took **76 seconds** for 93 rows, because a 0.029-degree latitude band is
scanned across every longitude in California and longitude is filtered in
memory.

`cma_comps.center` is built **from** the scalar pair, so it is complete by
construction and carries a 2dsphere index that bounds the query in both
dimensions. Use `$geometry`, not `$box`.

## Three empty column names, and two traps underneath

Measured on 6,120 CV type-A closings (trailing 12 months) and 3,376 actives. In
each case the obvious name is empty, so reading it yields a column of blanks
that looks like sparse MLS data rather than a wrong column:

| Wanted | Obvious field | Actual |
|---|---|---|
| Lot size | `lotSizeSqft` — 0% | `lotSizeArea` — 98.8% |
| Days on market | `daysOnMarket` — 0% | `closeDate − onMarketDate` |
| Pool | `poolPrivateYN` — 0% | `poolFeatures` — 76.6% |
| Street address | `address` — 0% | `unparsedAddress` — 99.9% |

`poolYn` is also 0% despite carrying an index, and `slug` holds the
`listingKey` rather than a readable slug — `slugAddress` is the readable one. `daysOnMarket` is a Mongoose
virtual computed in the Node layer, which is why `build-subdivision-cma.py`
already derives it.

Both replacements have a second trap.

### `lotSizeArea` mixes units

`lotSizeUnits` is populated on 26.5% of rows and says `Square Feet` 1,604 times
and `Acres` 16 times; the other 73.5% declare nothing. Read raw, the column
holds `0.2` and `16117` side by side and a median over it means nothing.

Resolution order is `lotSizeSquareFeet` (40.5%, unambiguous), then
`lotSizeAcres` (44.2%, unambiguous), then `lotSizeArea` disambiguated by
`lotSizeUnits` when declared and by magnitude when not. Note that
`lotSizeSquareFeet` and `lotSizeAcres` are frequently **`0.0` rather than
absent**, so a presence check is not enough — the guard is `> 0`.

The magnitude rule is safe because the distribution is bimodal with an empty
middle: 5,640 rows are ≥ 100, 61 are < 10, and only **3 rows in 6,120** land
between. A 10-square-foot lot does not exist and a 10-acre lot is 435,600 square
feet, so the two populations cannot be confused. Where both
`lotSizeSquareFeet` and `lotSizeArea` are present they agree on 1,849 rows and
differ by exactly a 43,560 factor on 17 — `lotSizeArea` was acres there.

`lotSizeSource` records which rule fired, so a disagreement stays auditable and
`area-cma.py` can warn when a parcel's lots are mostly inferred.

### `poolFeatures` has two serialisations, and presence is not a pool

3,105 rows are comma-joined strings (`Community, In Ground`) and 1,580 are dicts
(`{'in Ground': True, 'private': True}`). A parser that assumes a string
silently drops a third of the populated rows.

Presence is also not a private pool. Of the populated rows **2,136 are private,
2,002 are community or association only, and 275 say `None`**. Treating "has
`poolFeatures`" as "has a pool" calls two thousand condos with a shared pool
pooled homes — the most load-bearing attribute in a desert comp.

`poolKind` is therefore four-valued: `private`, `community`, `none`, `other`,
plus `unknown` when the field is absent. `other` is a real answer, not a
default — `Gunite, Heated` describes a pool without saying whose it is.
`private` wins when both appear, because a home with its own pool in a community
that also has one is priced on the private pool.

## `cma_comps` fields

`_id` is `cc:<c|a>:<listingKey>` — deterministic, carries no rank.

| Field | Meaning |
|---|---|
| `status` | `closed` or `active`; the discriminator that keeps this one query |
| `center`, `lat`, `lng` | the point, built from the scalar pair so it is never missing |
| `price` | `closePrice` for a sale, `listPrice` for a listing |
| `pricePerSqft` | `price ÷ livingArea`, null below a 100 sqft floor |
| `lotSizeSqft`, `lotSizeSource` | resolved square feet, and which rule produced it |
| `daysOnMarket`, `domSource` | date arithmetic, not datetime — see below |
| `poolKind`, `hasPrivatePool`, `spaKind` | four-valued, plus `unknown` |
| `beds`, `baths`, `yearBuilt`, `ageAtEvent`, `garageSpaces` | as fed |
| `eventDate`, `eventYear` | `closeDate` for a sale, `onMarketDate` for a listing |
| `saleToListRatio` | `closePrice ÷ originalListPrice`, closed only |
| `completeness` | fraction of nine comp fields present — the comp sort key |
| `city`, `postalCode`, `subdivisionName`, `county`, `address`, `slug` | place and display |

A row is dropped when it has no valid point, no positive price, or no event
date. Both exclusions are load-bearing: an unplaced sale cannot belong to a
parcel, and an unpriced one cannot be a comp.

`onMarketDate` is an ISO **string** while `closeDate` is a BSON datetime, so the
subtraction coerces one side and not the other. It also compares `.date()`, not
the datetimes: `onMarketDate` carries a wall-clock time and `closeDate` is
midnight, so subtracting datetimes floors a same-day close to −1 day. Fixing
that raised `daysOnMarket` coverage from 94.1% to 98.2% on a test city.

## What the read side returns

`closed` and `active` blocks, each with `count` and a `{median, avg, min, max,
p25, p75}` block per figure, plus `pool`/`spa` mixes and a private share.
`absorptionRate` is `(closed ÷ months) ÷ active`, matching the subdivision CMA.
`monthsOfInventory` is its reciprocal. `place` names the cities and subdivisions
the parcel actually touched, which is how a caller labels a rectangle that has
no name of its own.

`comps` lists up to five per side, ordered by `completeness` then recency. There
is **no scoring and no tiering** — those rank comps by similarity to a subject
and a parcel has none. `build-listing-cma.py` is the right tool the moment there
is a subject. The ordering rule and the `good`/`fair`/`insufficient` vocabulary
are `build-subdivision-cma.py`'s, so the two do not disagree about what "fair"
means.

`quality.notes` call out a parcel whose pool mix is mostly `unknown` or whose
lot sizes are mostly inferred, so a thin parcel cannot pretend to be a measured
one.

## Limits

- `min` and `max` are raw extremes and include non-arm's-length transfers; a
  La Quinta test parcel held a $10,000 type-A close. The median, `p25` and
  `p75` are the defensible figures, which is why they are reported alongside.
- A parcel over 25,000 rows is refused. A median across that many is describing
  several markets at once; draw smaller.
- `unified_in_escrow` is excluded, matching the subdivision CMA — a pending deal
  is a commitment in progress, not a market observation.
- Closed sales expire on a 5-year TTL (`closeDate_ttl_5years`,
  `expireAfterSeconds=157680000`), so the closed window has a hard horizon no
  matter what `--months` asks for.
- `cma_comps` is a snapshot. An active listing that closed since the last build
  sits in both sides until the next run; `builtAt` and the stale sweep bound how
  long.
- No PDF renderer here. This produces the numbers; rendering is the frontend's.
