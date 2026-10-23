# Sales heat maps — mapping by coordinate, not by name

Two collections make sales volume mappable. `area_geo` says where a named area
is. `sales_grid` says how much sold at a location, named or not. They are built
by `src/scripts/historical/build-area-geo.py` and
`src/scripts/historical/build-sales-grid.py`.

Nothing is geocoded and nothing is fetched. The closings already carry
`latitude`/`longitude` — 94% of Coachella Valley closings with a subdivision
name, and zero of them outside California. Both scripts only aggregate it.

## Why not group by subdivision

Because a third of the sales have no subdivision to group by, and grouping them
anyway produces a map that is confidently wrong.

31% of closings valley-wide carry a placeholder in `subdivisionName` — `Other`,
`Not Applicable`, `N/A` — and in CRMLS territory it is ~89% since the feed
stopped publishing the field in Sept 2025 (see `market-history.md`). Grouped by
name, every one of those collapses into a single bucket per city. `Other` in
Palm Desert is 586 sales plotted at one centroid with a 90th-percentile radius
of **1.49 miles** and a worst point **7.19 miles** out.

That bucket is not a place. It merges south Palm Desert with north Palm Desert,
and a map drawn from it shows one warm dot where there are really several
separate markets — or shows two unrelated areas as equally hot because both
happen to be unnamed. Measured independently: of the 381 CV subdivisions with
20+ sales, **14 have a p90 radius over 3 miles, and all 14 are placeholders.**

Grouping by coordinate removes the problem at the root. A sale's position does
not depend on whether an agent filled in a field.

## `area_geo` — where a named area is

One document per area, `_id` = `<scope>:<key>`, keyed exactly as
`market_history` keys the same area so a map is a join rather than a
computation. Scopes `zip`, `city`, `subdivision`.

| Field | Meaning |
|---|---|
| `centroid` | GeoJSON point, `[lng, lat]`, 2dsphere indexed |
| `lat` / `lng` | the same point as plain numbers |
| `bbox` | min/max lat and lng over the area's sales |
| `radiusMiles` | **p90** distance from centroid — the number to trust |
| `medianRadiusMiles`, `maxRadiusMiles` | the rest of the spread |
| `pointCount` | sales the geometry was built from |
| `placeholder` | true when the name is not a real subdivision |

The centroid is the **median** latitude and longitude, not the mean. A handful
of mis-geocoded points — Desert Princess has one 9.9 mi out — would drag a mean
and cannot move a median.

Check `radiusMiles` before trusting any centroid. Real communities are tight:
Sun City 1.18 mi, Sun City Shadow Hills 0.93, La Quinta Cove 0.86. City-scope
radii are legitimately larger (Palm Desert 4.50 mi) because a city is large; the
3-mile warning only means something at subdivision scope.

## `sales_grid` — how much sold at a location

One document per grid cell per property type. Default cell 0.005° ≈ 0.35 mi
north-south, 0.29 mi east-west at this latitude — well inside a real
subdivision, so a community spans several cells and a hot spot has a shape
instead of being a single dot. Larger cells start merging neighbours, which is
the failure the grid exists to avoid.

| Field | Meaning |
|---|---|
| `center`, `lat`, `lng`, `bbox` | the cell |
| `sales`, `salesByYear` | the heat weight |
| `medianClosePrice`, `medianPricePerSqft` | pooled across years in the cell |
| `subdivisionLabel` | dominant real name among the cell's named sales |
| `labelShare` | how dominant that name is |
| `namedShare` | **fraction of the cell's sales carrying any real name** |
| `nearestSubdivision`, `nearestSubdivisionMiles` | a pointer, not an assignment |
| `city`, `postalCode` | dominant values |

`namedShare` is the field that keeps the map honest. A cell at `0.08` is a real
hot spot whose sales simply have no names attached; label it by city and ZIP.
Do **not** promote `nearestSubdivision` into an attribution — it is the nearest
named centroid within 5 miles and nothing more, so a cell 0.2 mi from Sun City
can say so while a cell 3 mi from anything says that instead.

## Two numbers decide the caption

A label is safe to print only when both hold. They catch different failures.

**`namedShare`** — does this area have names at all? A cell at `0.02` is a real
hot spot whose sales carry no subdivision. Its `subdivisionLabel` can still be
populated: one named sale out of fifteen makes "Prado" the dominant name, and a
map that prints `label` without checking would caption all fifteen as Prado.

**`labelShare`** on a cell, **`topShare`** on a hot spot — does one community
actually dominate? This fails independently of the first. The Palm Springs 92264
zone is 97.3% named and still has no community worth naming: 221 sales across 31
cells with its leading name at 32, a `topShare` of 0.145. It is a hot
neighbourhood, not a hot subdivision.

Of the 55 Coachella Valley hot spots in 2026, only two clear a 0.5 `topShare` —
Sun City at 0.725 and Sun City Shadow Hills at 0.812. Those two are genuinely
single-community zones. The rest are mixtures and should be captioned by city
and ZIP, with `topSubdivisions` offered as "communities in this area" rather
than as the area's name.

| Condition | Caption |
|---|---|
| `namedShare` < ~0.2 | city + ZIP; ignore `label` entirely |
| `topShare` / `labelShare` < ~0.5 | city + ZIP; list `topSubdivisions` as contents |
| both healthy | the subdivision name |

`nearestSubdivision` is never a caption. It is the closest named centroid within
5 miles — useful for "near Sun City", wrong for "in Sun City".

## Hot spots are peaks, not contiguous blobs

`--hotspots` merges cells into the "top selling areas" view: a centre, a radius,
and a label per zone.

It is deliberately **not** a flood fill over touching cells. That was tried and
fails for the same reason name-grouping does, only geographically: at 0.005°
almost every cell in the developed valley touches another, so the fill chains
across neighbourhoods into 5-mile blobs — one run merged 853 sales of Palm
Springs and 722 of Rancho Mirage into single zones, putting Indian Wells and
south Palm Desert in one bucket.

Instead it takes the busiest unclaimed cell, absorbs everything within
`--radius` miles (default 1.25, which fits the largest real communities here
without reaching the next one), emits that zone, and repeats. Every cluster is
bounded by construction and centred on a real peak, so two adjacent busy
neighbourhoods stay two hot spots. The same 2026 Coachella Valley data yields
**55 zones of 1.0–1.7 mi** instead of three blobs — Palm Desert resolves into 9
separate hot spots while Indian Wells stays its own.

## `sales_hotspots` — the stored zone layer

Zones are precomputed by `build-sales-hotspots.py`, one document per zone per
year per property type plus an `allYears` companion, because clustering is a
calculation and the read surface never does those. Clustering per request would
also be non-deterministic across requests, so zones would shift and relabel as
a user pans.

Membership is clustered **once** on all-time sales and is identical in every
year's document; only the figures change. Clustering each year separately would
make zones appear, split and drift as a user drags a year slider, and every one
of those movements would be an artefact rather than a fact about the market.
Each year's document still carries its own centre, radius, medians and label,
weighted by that year's sales.

**Medians come from the sales, not the cells.** A zone's `medianClosePrice` is
the median over every closed sale in its member cells — not the mean or median
of those cells' `medianClosePrice` values, which is not a median at all once the
cells differ in size. The build therefore scans the closings once and derives
the medians, `namedShare` and `labelShare` from source. `sales_grid` only stores
those shares pooled across years, so a per-year figure could not come from it.

The caption is decided at build time and `labelKind` is stored, so the UI
renders a decision rather than re-deriving a rule:

| `labelKind` | When | Caption |
|---|---|---|
| `city_zip` | `namedShare` < 0.20 | `<City> <ZIP>` |
| `city_multiple` | `labelShare` < 0.50 | `<City> — multiple communities` |
| `subdivision` | both healthy | the subdivision name |

`zoneId` is derived from the seed cell and is stable across years and across
builds; `_id` carries rank because that is what the tab sorts on, so it moves
when volume moves. Track a zone with `zoneId`, fetch with `_id`.

`hs:meta` holds the clustering parameters so any output is reproducible and a
zone split can be explained. It deliberately carries **no top-level field the
read routes filter on** — `propertyType` and `scope` live inside `params`.
With `propertyType` at the top level it matched `find({propertyType: "A"})`, so
any route that filtered without `year` would have served the parameters block as
though it were a zone. The stale sweep also excludes it by `_id`, or it would be
deleted and recreated on every build and report a phantom removal.

Indexes: `propertyType+year+rank` (the top-N query), `center` 2dsphere
(viewport), `city+year` (the side list).

### Querying it: `$geometry`, never `$box`

A `2dsphere` index does not serve `$box` — that is a legacy `2d` operator.
Measured on `sales_grid`:

| Query | Keys examined | Docs examined | Time | Plan |
|---|---|---|---|---|
| `$geoWithin: { $box }` | 0 | 34,082 | 249 ms | COLLSCAN |
| `$geoWithin: { $geometry }` | 1,059 | 1,047 | 18 ms | IXSCAN |

Both returned the same 869 rows. Use a Polygon via `$geometry` for every
viewport query, on this collection and on `sales_grid`.

## Running it

Order matters: the grid reads `area_geo` for its nearest-area pointer, and the
zones read the grid.

```bash
python3 src/scripts/historical/build-area-geo.py --cv
python3 src/scripts/historical/build-sales-grid.py --cv
python3 src/scripts/historical/build-sales-hotspots.py --cv

# whole feed
python3 src/scripts/historical/build-area-geo.py --all
python3 src/scripts/historical/build-sales-grid.py --all

# what is stored
python3 src/scripts/historical/build-area-geo.py --status
python3 src/scripts/historical/build-sales-grid.py --status
```

Exports for a frontend:

```bash
# cells for a heat layer
python3 src/scripts/historical/build-sales-grid.py --export cells.json --cv --year 2026

# hot zones with a centre, radius and label
python3 src/scripts/historical/build-sales-grid.py --hotspots hot.json \
    --cv --year 2026 --min-sales 10

# named-subdivision centroids joined to market_history counts (GeoJSON)
python3 src/scripts/historical/build-area-geo.py --geojson subs.geojson \
    --cv --year 2026
```

Both collections are rebuildable from the closings at any time, so neither needs
the shrink guard `market_history` carries. Re-running is idempotent: `_id` is
derived from the area key or the grid index.

## Reading a year

`salesByYear` lets a map animate, but 2026 holds 11 of 12 months, so the current
year is always short. Compare completed years, or prorate and say so.
