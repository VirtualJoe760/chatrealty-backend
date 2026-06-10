# Listing-Level CMA — Frontend Integration Guide

**Audience:** the Claude (or human) working on the frontend repo who needs to consume the pre-computed listing-level CMA produced by the backend.
**Status:** backend pipeline live as of 2026-05-04. Riverside fully backfilled (7,073 listings); other counties pending.
**Backend script:** `src/scripts/cma/build-listing-cma.py` (this VPS).
**Companion doc:** `docs/cma/SUBDIVISION_AND_CITY_CMA.md` — same pattern, different scope.

---

## TL;DR

For every active residential listing, a `cmaStats` subdocument is now persisted directly on the listing in `unified_listings`. The frontend's CMA route can become a `findOne` lookup instead of a 1–20s on-demand call to the engine.

```ts
// Replace the entire fetch flow in CMAReport.tsx:
const listing = await UnifiedListing.findOne(
  { listingKey },
  { cmaStats: 1, /* + your existing projection */ }
);
return Response.json(listing.cmaStats);
```

When `cmaStats` is missing or stale, fall back to the existing on-demand engine.

---

## Where the data lives

- **Database:** `admin` (yes, app data is in `admin` — see `docs/db/cleanup-and-migration.md` for the planned migration to `jpsrealtor`).
- **Collection:** `unified_listings`
- **Field:** `cmaStats` (a single embedded subdocument on each listing)
- **Mongoose model addition required:** `cmaStats: { type: Schema.Types.Mixed }` on the listing schema. The Python builder uses PyMongo and bypasses Mongoose; without the schema declaration, Mongoose's `strict: true` will silently drop the field on reads through `.lean()` or write-throughs. Add the field, even as `Mixed`, before pulling into the Node side.

The legacy `unifiedlistings` (no underscore) name is a **MongoDB view** that pass-throughs to `unified_listings`. Both work; prefer the underscore form for direct queries.

---

## Why pre-computed

Currently the frontend hits `/api/cma/generate?listingKey=...` and the server runs the full engine: 5-level hierarchy search, 12-factor scoring, narrative generation. That's 1–20 seconds depending on tier and cache state. Pre-computation reduces that to a single document fetch (<50ms).

The two systems share an algorithm by design — the Python builder is a faithful port of the TypeScript engine. **If the on-demand engine and the pre-computed `cmaStats` ever disagree, the on-demand version is the spec.** Re-run the backfill against any listing where you spot a divergence.

---

## Full schema

```ts
interface CmaStats {
  // ── Run metadata ─────────────────────────────────────────────────────────
  lastUpdated: string;                  // ISO-8601 UTC; the freshness check below
  tier: "affordable" | "residential" | "luxury";

  // ── The subject (the listing itself, pre-extracted) ──────────────────────
  subject: {
    listPrice: number;                  // 0 if missing
    livingArea: number;
    lotSize: number;                    // sqft
    bedsTotal: number;
    bathsTotal: number;                 // float (e.g. 3.5)
    yearBuilt: number;
    pricePerSqft: number;
    propertyType: "A";                  // residential sale
    propertySubType: string;            // e.g. "Condominium", "Single Family Residence"
    landType: "Fee" | "Lease";
    pool: boolean | null;               // null = unknown
    spa: boolean | null;
    view: "golf" | "mountain" | "desert" | "water" | "city" | null;
    garageSpaces: number;
  };

  // ── Top 5 active comps (sorted desc by similarityScore) ──────────────────
  activeComps: Array<{
    listingKey: string;                 // join back to unified_listings if you need more
    address: string;                    // already concatenated street parts
    city: string;
    subdivisionName: string | null;
    listPrice: number;
    livingArea: number;
    bedsTotal: number;
    bathsTotal: number;
    yearBuilt: number;
    listPricePerSqft: number;
    daysOnMarket: number;
    similarityScore: number;            // 0–100; higher = better match
    pool: boolean | null;
    spa: boolean | null;
    view: string | null;                // category, like subject.view
    garageSpaces: number;
    landType: "Fee" | "Lease";
    lotSize: number;
  }>;

  // ── Top 5 closed comps ───────────────────────────────────────────────────
  closedComps: Array<{
    listingKey: string;
    address: string;
    city: string;
    subdivisionName: string | null;
    closePrice: number;
    closeDate: string;                  // YYYY-MM-DD
    listPrice: number;
    livingArea: number;
    bedsTotal: number;
    bathsTotal: number;
    yearBuilt: number;
    salePricePerSqft: number;
    salePriceToListRatio: number | null;  // e.g. 0.965 (closePrice / listPrice)
    daysOnMarket: number;
    similarityScore: number;
    pool: boolean | null;
    spa: boolean | null;
    view: string | null;
    garageSpaces: number;
    landType: "Fee" | "Lease";
    lotSize: number;
  }>;

  // ── Aggregate stats over the top 5 of each ───────────────────────────────
  stats: {
    active: {
      count: number;                    // ≤ 5
      avgPrice: number;
      minPrice: number;
      maxPrice: number;
      medianPrice: number;
      avgPricePerSqft: number;
      avgSqft: number;
      avgDaysOnMarket: number;
      avgLotSize: number;
    };
    closed: {
      count: number;
      avgPrice: number;
      minPrice: number;
      maxPrice: number;
      medianPrice: number;
      avgPricePerSqft: number;
      avgSqft: number;
      avgDaysOnMarket: number;
      avgLotSize: number;
      avgSalePriceToListRatio: number | null;  // e.g. 0.972
    };
  };

  // ── How the comp search went ─────────────────────────────────────────────
  searchCriteria: {
    levelsUsed: { active: number; closed: number };  // 1–5; see below
    subdivisionMatched: boolean;        // true if comps came from L1 or L2
    totalCandidatesEvaluated: { active: number; closed: number };
  };

  // ── Human text ───────────────────────────────────────────────────────────
  narrative: string;                    // 1–4 sentences, deterministic templating
  limitations: string[];                // confidence warnings — render as caveats
  inferences: string[];                 // attributes inferred from remarks vs MLS fields

  // ── Confidence ───────────────────────────────────────────────────────────
  quality: {
    confidence: "high" | "good" | "medium" | "low" | "insufficient";
    activeCompCount: number;
    closedCompCount: number;
  };
}
```

### Confidence rubric

`confidence` derives from `activeCompCount + closedCompCount`:

| Total comps | confidence |
|---:|---|
| ≥ 8 | `high` |
| 5–7 | `good` |
| 3–4 | `medium` |
| 1–2 | `low` |
| 0 | `insufficient` |

Render UI affordances accordingly:
- `high` / `good` → show full report, no warnings.
- `medium` → show report, surface `limitations[]` prominently.
- `low` → show report with a "limited data" banner.
- `insufficient` → fall back to the on-demand engine, or render a "no comps available" state. Don't display empty stat blocks.

### Search level meaning

`searchCriteria.levelsUsed.active` and `.closed` tell you *how* the comps were found:

| Level | Where comps came from |
|---|---|
| 1 | Same subdivision, tight match (sqft ±300–600, beds ±1, pool match) |
| 2 | Same subdivision, relaxed sqft |
| 3 | Same city, tight match (no subdivision constraint) |
| 4 | Same city, relaxed sqft |
| 5 | Geographic radius fallback (`$nearSphere`, 2–5 miles by tier) |

Use this to render UI like *"Comps from this subdivision"* (L1/L2) vs *"Comps from nearby"* (L4/L5). When `subdivisionMatched === false`, lead with that — users care.

### Tier

`tier` reflects the listing's price+size bucket and changes the algorithm's tolerances:
- `affordable`: listPrice < $500K **or** livingArea < 1500 sqft
- `luxury`: listPrice > $1.5M **or** livingArea > 3000 sqft
- `residential`: everything else

Luxury comps use wider sqft tolerances and longer lookback windows. The narrative includes a "luxury — fewer comparable sales widen the confidence range" line for that tier.

### Inferences

`inferences[]` flags attributes that came from `publicRemarks` regex parsing rather than structured MLS fields. Examples:

```json
[
  "Pool inferred from listing remarks (present).",
  "Spa inferred from listing remarks (present).",
  "View category 'mountain' inferred from listing remarks.",
  "Garage spaces (3) inferred from listing remarks."
]
```

These are typically rendered as a small "data confidence" footnote, not as a primary UI element. The subject's `pool`/`spa`/`view`/`garageSpaces` are populated either way; `inferences[]` is purely the audit trail.

### Limitations

`limitations[]` is a list of human-readable warnings. Possible entries:
- `"Only N active comparable(s) found."`
- `"Only N closed comparable(s) found."`
- `"Insufficient comparables in {subdivisionName} — search expanded beyond the subdivision."`
- `"Pool mismatch: subject has a pool but most closed comps do not."`
- `"Closed comps required an extended lookback period."`
- `"Land-type mismatch: N of N closed comps are lease-land."`

Render these as bullet warnings in the report.

---

## Real example

Source listing: `542 Red Arrow Trail, Palm Desert, CA 92211` (Indian Ridge), `listingKey: 20250422203044846188000000`.

```json
{
  "lastUpdated": "2026-05-02T22:00:39.620820+00:00",
  "tier": "residential",
  "subject": {
    "listPrice": 749999, "livingArea": 2182, "lotSize": 2182,
    "bedsTotal": 3, "bathsTotal": 4.0, "yearBuilt": 1993,
    "pricePerSqft": 344, "propertyType": "A",
    "propertySubType": "Condominium", "landType": "Fee",
    "pool": null, "spa": null, "view": "golf", "garageSpaces": 2
  },
  "activeComps": [
    { "listingKey": "...", "address": "703 Box Canyon Trail",
      "listPrice": 1295000, "livingArea": 2368, "similarityScore": 92.39, "...": "..." },
    { "listingKey": "...", "address": "416 Desert Holly Drive",
      "listPrice": 879000, "livingArea": 2182, "similarityScore": 92.13, "...": "..." }
  ],
  "closedComps": [
    { "listingKey": "...", "address": "440 Desert Holly Drive",
      "closePrice": 1050000, "closeDate": "2026-04-28",
      "salePriceToListRatio": 0.985, "similarityScore": 91.75, "...": "..." }
  ],
  "stats": {
    "active": { "count": 5, "medianPrice": 910000, "avgPricePerSqft": 451, "...": "..." },
    "closed": { "count": 5, "medianPrice": 940000, "avgSalePriceToListRatio": 0.984, "...": "..." }
  },
  "searchCriteria": {
    "levelsUsed": { "active": 1, "closed": 2 },
    "subdivisionMatched": true,
    "totalCandidatesEvaluated": { "active": 7, "closed": 13 }
  },
  "narrative": "Listed 20.2% below the median closed sale of $940,000. Price per sqft ($344) is a 18% discount to closed comps ($417).",
  "limitations": [],
  "inferences": [],
  "quality": { "confidence": "high", "activeCompCount": 5, "closedCompCount": 5 }
}
```

Notice:
- Active comps came from L1 (same subdivision, tight match), closed from L2 (relaxed). `subdivisionMatched: true` reflects that.
- 7 active candidates and 13 closed were considered before keeping the top 5 each.
- `pool: null` for the subject means neither the MLS field nor the remarks were definitive — the frontend should *not* render "no pool", just omit the affordance.

---

## How to fetch it

### Server component (pages)

```ts
// app/mls-listings/[slug]/page.tsx
const listing = await UnifiedListing.findOne(
  { slugAddress: slug },
  /* projection */
).lean();

// Pass cmaStats to the client component
return <ListingClient listing={listing} cmaStats={listing.cmaStats ?? null} />;
```

### Client component (CMA report)

```tsx
// CMAReport.tsx — shorten the existing useEffect
function CMAReport({ listingKey, cmaStats: precomputed }: Props) {
  const [data, setData] = useState(precomputed);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (data) return;                         // already have pre-computed
    setLoading(true);
    fetch(`/api/cma/generate?listingKey=${listingKey}`)
      .then(r => r.json())
      .then(setData)
      .finally(() => setLoading(false));
  }, [listingKey, data]);

  if (loading) return <CMASkeleton />;
  if (!data) return <EmptyCmaState />;
  // ... render data ...
}
```

### Freshness check (optional)

If you want to ignore stale pre-computed data and force the live engine:

```ts
const STALE_HOURS = 96;
const fresh =
  cmaStats?.lastUpdated &&
  Date.now() - new Date(cmaStats.lastUpdated).getTime() < STALE_HOURS * 3600_000;
const initial = fresh ? cmaStats : null;
```

The cron runs twice a week (Mon + Thu at 1 AM), so listings get refreshed every 60–84h. A 96h staleness threshold absorbs one missed run.

---

## Field-level differences vs. the on-demand engine

If you've been consuming the on-demand `CMAResult` shape, three small mappings:

| On-demand engine field | Pre-computed `cmaStats` field |
|---|---|
| `result.subject.*` | identical shape — same field names |
| `result.activeComps[i].score` | `cmaStats.activeComps[i].similarityScore` |
| `result.searchPath` (string list) | `cmaStats.searchCriteria.levelsUsed` (numeric) |

Otherwise the shapes are aligned — port should be near-mechanical.

---

## Backfill status

| County | Active listings | Status |
|---|---:|---|
| Riverside | 8,144 | ✅ Done (2026-05-03) |
| Los Angeles | 14,242 | Pending |
| San Bernardino | 5,673 | Pending |
| Orange | 4,385 | Pending |
| San Diego | 2,526 | Pending |
| Contra Costa | 1,696 | Pending |
| Alameda | 1,432 | Pending |
| Ventura | 1,321 | Pending |
| Other | < 1,000 each | Pending |

Backfill runs in the background. Until each county is done, those listings hit the existing fallback path (on-demand `/api/cma/generate`). No frontend change needed — the fallback handles it.

After full backfill, the cron at `0 1 * * 1,4` keeps it fresh. Listings whose `cmaStats.lastUpdated` is < 60h old are skipped (the script's `--skip-fresh-hours` flag).

---

## Edge cases

- **`cmaStats` field absent.** Listing was added after the last cron run, or the cron hasn't reached its county yet. Fall back to the on-demand engine.
- **`quality.confidence === "insufficient"`.** Comps came up empty. Either render "no comparable sales available" or fall back to the on-demand engine (which may have looser fallbacks).
- **`activeComps` or `closedComps` shorter than 5.** Genuinely sparse market. The `count` and `quality.confidence` already reflect this.
- **`subject.pool: null` (or any other attribute).** Unknown — neither MLS field nor remarks were definitive. Don't render either "has pool" or "no pool"; just skip the affordance.
- **`searchCriteria.subdivisionMatched: false`.** Comps weren't pulled from this subdivision. If you display "comps from {subdivision}", suppress that header — show "comps from nearby" instead.
- **`stats.closed.avgSalePriceToListRatio: null`.** No usable list-vs-close pricing pairs. Suppress that affordance.

---

## Related backend docs

- `src/scripts/cma/build-listing-cma.py` — the builder
- `docs/cma/SUBDIVISION_AND_CITY_CMA.md` — the same pattern at subdivision scope
- `src/scripts/cma/README.md` — operational notes for both builders
- `docs/db/current-state.md` — what's in the DB more broadly
