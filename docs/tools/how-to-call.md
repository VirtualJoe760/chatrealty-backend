# How to call this data

**Read this before writing any MCP tool.** It answers the two questions an
implementer has: *where does the value come from*, and *what exactly do I run to
get it*.

---

## 1 · There is exactly one access mode: read MongoDB

**No script on the backend VPS is callable on demand. There is no HTTP API.**

Verified 2026-10-14: the VPS listens on SSH (22) and a local DNS resolver only.
No web server, and `requirements.txt` contains no web framework — just `pymongo`,
`requests`, `python-dotenv` and parsing helpers.

```
  ┌────────────────────────────────────────────────────┐
  │ VPS (this repo)                                    │
  │   Python scripts ── run by CRON ONLY ──┐           │
  │   not reachable over the network       │           │
  └────────────────────────────────────────┼───────────┘
                                           ▼ writes
                              ┌────────────────────────┐
                              │   MongoDB  (db: admin) │
                              └────────────┬───────────┘
                                           ▲ reads
  ┌────────────────────────────────────────┼───────────┐
  │ Frontend repo: MCP tool ───────────────┘           │
  │   one findOne / one indexed find. No math.         │
  └────────────────────────────────────────────────────┘
```

So for **every** tool:

> The value is already in MongoDB, or it does not exist yet. If it does not
> exist, a builder script has to be written here first and scheduled — the tool
> cannot trigger a computation.

If you ever genuinely need on-demand computation, that requires a new HTTP
service on the VPS that does not exist today. Don't design around one, and
don't try to SSH or shell out from a tool.

## 2 · Connection

```ts
// Same cluster the Python builders write to.
const uri = process.env.MONGODB_URI;
// IMPORTANT: use the URI's DEFAULT database. Do not hardcode a name.
// It currently resolves to `admin` (migration tracked in
// docs/db/cleanup-and-migration.md — code that hardcodes "admin" breaks on
// migration day; code that uses the default does not).
const db = client.db();
```

**Mongoose `strict: true` silently drops every field the Python builders write**,
because they use PyMongo and bypass Mongoose. Declare them or `.lean()` returns
nothing — the exact schema additions are in [README.md](README.md#four-things-that-will-bite-you).

## 3 · Lookup keys, and how to resolve user words into them

Everything is keyed by one of four identifiers. All four are **unique indexes**,
verified 2026-10-14.

| Target | Key | Unique index | Example |
|---|---|---|---|
| subdivision | `slug` | `slug_1` ✅ | `"palm-desert-country-club"` |
| subdivision | `normalizedName` + `city` | `normalizedName_1_city_1` ✅ | `"palm desert country club"` + `"Palm Desert"` |
| city | `slug` | `slug_1` ✅ | `"palm-desert"` |
| ZIP | `postalCode` | `postalCode_1` ✅ | `"92211"` **(string)** |
| listing | `listingKey` | `listingKey_unique` ✅ | `"20241106010452172739000000"` |

### ⚠ `normalizedName` alone is NOT unique

152 subdivisions are named `"other"`, 10 `"downtown"`, 4 `"fallbrook"`,
3 `"country club estates"`. **Name-only resolution will return the wrong
community.** You must pair it with `city` — which is exactly why the compound
unique index exists.

```ts
// WRONG — may match any of 152 docs
db.collection("subdivisions").findOne({ normalizedName: "other" });

// RIGHT — unique
db.collection("subdivisions").findOne({
  normalizedName: name.trim().toLowerCase(),
  city: cityName,
});
```

### Resolving a name to a slug

`normalizedName` is simply the lowercased `name`. Prefer taking a `slug` as the
tool parameter and exposing a separate resolver tool, so the AI never guesses an
identifier:

```ts
// resolve_subdivision(name, city) -> { name, slug, city, dominantPostalCode }
db.collection("subdivisions").findOne(
  { normalizedName: name.trim().toLowerCase(), city },
  { projection: { name: 1, slug: 1, city: 1, dominantPostalCode: 1, _id: 0 } }
);
// → { name: "Palm Desert Country Club", slug: "palm-desert-country-club",
//     city: "Palm Desert", dominantPostalCode: "92211" }
```

For fuzzy input, `subdivisions` has a text index on
`name` / `description` / `keywords` (`name_text_description_text_keywords_text`)
— use it to offer choices, then look up by the chosen `slug`.

### `postalCode` is a string

`"92211"`, never `92211`. A numeric query silently returns nothing.

## 4 · Worked example, end to end

*"Would anything in Palm Desert Country Club cash-flow as a rental?"*

```ts
// Step 1 — resolve the name to a slug (skip if the AI already has one)
const sub = await db.collection("subdivisions").findOne(
  { normalizedName: "palm desert country club", city: "Palm Desert" },
  { projection: { slug: 1, _id: 0 } }
);
if (!sub) return { found: false, reason: "no such subdivision in that city" };

// Step 2 — ONE findOne for the entire answer. No aggregation, no math.
const doc = await db.collection("subdivisions").findOne(
  { slug: sub.slug },
  { projection: { cashflowProfile: 1, _id: 0 } }
);

// Step 3 — return the block whole; its caveats are inside it
if (!doc?.cashflowProfile) {
  return { available: false,
           reason: "no active inventory with a rent signal in this subdivision" };
}
return doc.cashflowProfile;
```

That returns four rental strategies, per-bedroom economics, rent provenance,
confidence and plain-language caveats — in two queries and a few KB. The same
answer computed by the AI costs tens of thousands of tokens.

## 5 · Handling the absent case

Coverage is never 100% and that is correct — thin markets genuinely lack comps.
**Never substitute a zero or a guess for a missing block.** Every block either
exists with its own `quality.confidence`, or is absent and means "we don't
know".

| Missing | Means | Say |
|---|---|---|
| `cashflowProfile` | no active inventory with a rent signal | offer the parent city via `geo.city` |
| `cashflowStats` on a listing | no rent signal for its area | "we can't estimate rent here", not "$0" |
| `rentStats.seasonal` | too few tiered lease comps | the ZIP fallback already ran; if also absent, say so |
| `rentStats.closedSeasonal` | too few closed seasonal leases | same |
| `poolPremium` | no private-vs-no-pool contrast to measure | "unavailable", never estimated |

## 6 · Per-block retrieval

Each doc has a **How to get it** section with the exact query, the key, what to
return and the empty case:

| Block | Doc |
|---|---|
| `cashflowProfile` | [cashflow-profile.md](cashflow-profile.md#how-to-get-it) |
| `cashflowStats` | [listing-cashflow.md](listing-cashflow.md#how-to-get-it) |
| rent layers | [rent-rates.md](rent-rates.md#how-to-get-it) |
| `inventoryProfile` | [inventory-profile.md](inventory-profile.md#how-to-get-it) |
| `cmaStats` | [cma-comps.md](cma-comps.md#how-to-get-it) |

For list / rank / write tools — which mostly need an index rather than a
precomputed block — see the four shapes in
`docs/strategies/tool-development.md`.
