# `inventoryProfile` — what is actually for sale here

**Producer:** `src/scripts/cma/build-inventory-profile.py`
**Lives on:** `subdivisions` and `cities`, keyed by `slug`
**Coverage:** 742 of 1,426 subdivisions · 836 of 1,047 cities *(2026-10-14)*
**Refreshed:** Tue + Fri 2 AM (stage 4, before `build-cashflow-profile.py`)
**Backs:** no tool yet — also consumed internally by `cashflowProfile.inventory`

---

## What it is

The two things a cash-flow or "what's available" answer needs that
`build-subdivision-cma.py` does not produce:

1. **`bedMix` — a histogram, not an average.** `cmaStats.active` carries only
   `avgBeds` (e.g. 2.88) and `typicalBeds` (3), which cannot answer *"how many
   2/2 versus 3/2 are for sale?"*
2. **Promoted profile prevalences.** Pool / spa / gated / senior fields exist
   deeper in `cmaStats.subdivisionProfile`; this surfaces them with explicit
   `withData` denominators.

```js
db.subdivisions.findOne({ slug: "palm-desert-country-club" }, { inventoryProfile: 1 })
db.cities.findOne({ slug: "palm-desert" }, { inventoryProfile: 1 })
```

## Shape

```ts
interface InventoryProfile {
  lastUpdated: string;
  scope: "subdivision" | "city";
  geo: { subdivisionName?; slug; city; county };
  activeCount: number;

  bedMix: Array<{
    beds: number; bedsLabel: string;   // "5" means 5+ (bucket cap)
    count: number; pct: number;
    medianListPrice: number; medianSqft: number; medianBaths: number;
  }>;
  bedMixBinned: number;

  poolPrevalence: number;              // PRIVATE pools only
  poolPrevalenceAny: number;           // private + community
  poolPrevalenceCommunity: number;
  poolBreakdown: { private; community; none; unknown; withData; total };

  spaPrevalence:    { pct; withData; total };
  gatedCommunity:   { pct; withData; total };
  seniorCommunity:  { pct; withData; total };
  dominantSubType: string;             // e.g. "Single Family Residence"
}
```

## Reading rules

1. **Three pool numbers, three meanings.** `poolPrevalence` is **private** pools
   only. A community where 57% of pools are association-owned and 36% private is
   a materially different rental proposition from one where every home has its
   own pool — short-term-rental demand and the private-pool rent premium both
   hinge on it. Always say which you are quoting.
2. **`withData` is the real denominator.** MLS feeds are patchy on these fields.
   `{ pct: 0.0, withData: 15, total: 15 }` is a confident zero;
   `{ pct: 0.0, withData: 2, total: 40 }` is noise. Check it before asserting.
3. **`bedsLabel: "5"` is a 5+ bucket**, not exactly five bedrooms.
4. **`pct` is a fraction** (0.2 = 20%), consistent across the codebase.
5. **Pair it with price context.** Each `bedMix` entry carries its own
   `medianListPrice` and `medianSqft`, so "the 3-bedrooms here run about
   $545,500 at 1,501 sqft" needs no extra query.

## Why it is separate from `cashflowProfile`

`cashflowProfile.inventory` already embeds these fields, so an area cash-flow
answer needs only one `findOne`. This block stays independently readable for the
many questions that are about **supply**, not economics — *"what's for sale in
PDCC?"*, *"are there any 4-bedrooms?"*, *"is it gated?"* — none of which should
have to load a cash-flow rollup.

## Suggested MCP tool

```
get_inventory_mix(slug, scope?: "subdivision" | "city")
  → the full inventoryProfile block
```

Useful immediately for qualifying buyer conversations before any cash-flow
question comes up. Absent block ⇒ no active inventory; fall back to the parent
city via `geo.city`.
