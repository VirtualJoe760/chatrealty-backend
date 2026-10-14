# Strategy — Tool Development

**Who this is for:** anyone adding an AI capability to chatrealty, on either side
of the repo split.

**How the split works.** This repo (the VPS) holds Python scripts and the
database. The MCP tools live in the frontend repo. The scripts here prepare
answers and keep the data clean; the MCP tools look those answers up. Neither
the tool nor the AI does analysis.

**The whole idea in one sentence:** if the AI has to think about it, we got it
wrong — it should be looking something up.

---

## Why this matters

Ask the AI *"would this Palm Desert condo cash-flow as a rental?"* and, with no
tool to call, it will try to do the job itself: pull comps, pull lease listings,
guess a rent, guess taxes and insurance, amortize a loan, and narrate the math.
That costs tens of thousands of tokens, takes 30 seconds, gives a different
answer every time, and when it's wrong you can't tell whether the data or the
AI's arithmetic was at fault.

With a tool, that same question is one database lookup and a few hundred tokens.

But **not every tool needs precomputing.** "How many subdivisions are in Palm
Desert" doesn't need a nightly job — it needs one indexed query. Knowing which
kind of tool you're building is the first decision, and it changes the rules.

---

## The four shapes of tool

| Shape | Example question | How it works | Needs a script here? |
|---|---|---|---|
| **1. Look up** | "Would this cash-flow?" | read a precomputed block | **Yes** — analysis must happen offline |
| **2. List** | "What subdivisions are in Palm Desert?" | indexed `find` + tight projection | **No** — if the index exists |
| **3. Rank** | "Biggest house? Longest on market?" | indexed `find` + `sort` + `limit` | **Sometimes** — see below |
| **4. Write** | "Create a new subdivision" | validated insert/update | **Yes** — a bad write poisons every later answer |

Most token waste is not from missing precompute. It's from **returning too much
JSON**. Sending 50 full listing documents to answer "which is the biggest house"
is ~200 KB; sending one record with eight fields is ~200 bytes. Shapes 2 and 3
save tokens mostly through tight projections, not through precomputing.

---

## Shape 1 — Look up a precomputed answer

Use this when answering requires *analysis*: medians, comp selection, financing
math, confidence judgments.

A script here does all of it on a schedule and saves a self-describing block on
a document. The block holds the figures **and** everything needed to caveat them
— sample sizes, where the data came from, confidence, and plain-language notes.
The tool returns the block whole.

Example that already exists: `cashflowProfile` on `subdivisions` and `cities`.
One `findOne` by slug returns four rental strategies, per-bedroom economics,
rent provenance and caveats. See `docs/tools/cashflow-profile.md`.

## Shape 2 — List the options

Use this for "what are my choices here" questions. **Usually needs no new
script.** Your example — all subdivisions in a city — is already a fast indexed
query, because `subdivisions` is indexed on `city`:

```js
db.subdivisions
  .find({ city: "Palm Desert" }, { name: 1, slug: 1, listingCount: 1, _id: 0 })
  .sort({ name: 1 })
// 90 subdivisions in Palm Desert
```

Rules for list tools:

- **Project only the fields the AI needs.** A subdivision document is large;
  `name` + `slug` + a count is usually all a picker needs.
- **Always return the `slug`**, so the AI can chain into a detail tool without
  guessing an identifier.
- **Sort deterministically** (name, or count descending). An unsorted list
  changes between calls and makes the AI look unreliable.
- **Cap the result and say you capped it.** 90 is fine to return; 1,400 is not.
- **Confirm the index exists** before shipping, or it collscans.

## Shape 3 — Find the best or worst by a stat

"Biggest house", "longest on market", "lowest price per square foot." These are
sorts, not analysis — *provided* the thing you're sorting by is a real, indexed,
populated field. Checked against production on 2026-10-14, that is not always
true:

| Question | Field to sort | Status today |
|---|---|---|
| Biggest house | `livingArea` desc | ✅ populated (99.8%); needs a `city + livingArea` index |
| Longest on market | `onMarketDate` **asc** | ✅ populated (100%) and indexed |
| Most expensive | `listPrice` desc | ✅ populated and well indexed |
| Lowest price per sqft | — | ❌ **no such field exists** — needs a script |

Three things to understand here:

**Oldest on-market date *is* longest days-on-market.** You don't need to compute
or store DOM — sorting `onMarketDate` ascending gives the identical ordering and
uses an existing index. Prefer a proxy you can index over a value you have to
calculate.

**Price per square foot has to be precomputed.** It's `listPrice / livingArea`,
and a database cannot index a value it doesn't store. Either a script writes a
`pricePerSqft` field (then it's indexable and this becomes a plain sort), or
every query aggregates across 47,317 listings. Write the field.

**Superlatives land on your worst data.** This is the big one. The honest
answer to "cheapest sale listing in Palm Desert" today is:

```
$795      800 sqft   77955 Calle Las Brisas South     ← misclassified rental
$12,500  1383 sqft   43712 Avenida Alicante           ← bad data
$210,000  522 sqft   73850 Fairway Drive, 14          ← the real answer
```

A sort by price has no judgment. It will hand the AI a $795 house and the AI
will report it confidently. `build-cashflow.py` guards this with a $25,000
floor; **every ranking tool needs equivalent guards** — a price floor, a
non-zero `livingArea`, a plausible range. Without them, the tool's headline
result is its worst record.

**Also: don't trust an index to mean a field exists.** `unified_listings` has
indexes named `daysOnMarket_listPrice`, `beds_baths_listPrice` and
`city_beds_listPrice`, but `daysOnMarket`, `beds` and `baths` are **0%
populated** — the real fields are `bedsTotal` and `bathroomsTotalInteger`.
Sorting by `daysOnMarket` returns nothing useful. Verify coverage before you
sort on anything.

## Shape 4 — Create or change a record

Writes are different in kind: a bad write doesn't give one wrong answer, it
poisons every answer that reads it afterwards. Your "create a new subdivision"
tool lives here.

**What a subdivision document needs to actually work:**

```js
{
  name: "Palm Desert Country Club",      // must match the MLS spelling
  slug: "palm-desert-country-club",       // lowercase, hyphenated, unique
  normalizedName: "palm desert country club",  // lowercase name
  city: "Palm Desert",
  county: "Riverside",
  region: "Coachella Valley",
  coordinates: { latitude: 33.7364, longitude: -116.3178 },
  hasManualData: true                     // marks it hand-created
}
```

**The trap that will silently break it:** `build-subdivision-cma.py` finds a
subdivision's listings by **case-insensitive exact match** of
`unified_listings.subdivisionName` against the subdivision's `name`. So if you
create *"Palm Desert CC"* but the MLS writes *"Palm Desert Country Club"*, the
record is valid, looks fine, and gets **zero comps forever**. Before creating
one, check what the MLS actually calls it:

```js
db.unified_listings.distinct("subdivisionName", { city: "Palm Desert" })
```

Rules for write tools:

- **Upsert on `slug`, never blind-insert.** If the tool is called twice — a
  retry, a double-click — you get two subdivisions competing for the same
  listings. Make the write idempotent.
- **Derive `slug` and `normalizedName` from `name`**, don't accept them as
  input. Two spellings of the same community is the failure mode.
- **Reject placeholder names.** `"Other"`, `"None"`, `"N/A"`,
  `"Not in a Development"` and similar appear in the MLS where there's no real
  community name. They group unrelated properties, so the builders exclude them.
  A subdivision created with one of these names is a bucket of strangers.
- **Set `hasManualData: true`** so later automated passes know a human authored
  it and don't clobber it.
- **Confirm the write.** Return what was stored and whether it was a create or
  an update. "Done" is not a result.
- **Never let the AI invent geography.** County, region and coordinates should
  come from the city record or a geocode, not from the model's memory.
- **A new record is empty until the builders run.** `cmaStats`, `rentStats` and
  `cashflowProfile` appear on the next cron pass, not immediately. Say so, or
  the next question about it returns nothing and looks broken.

---

## The rules, plainly

These eight apply across all four shapes. Each one already cost us a real bug.

**1. The script does the math, never the AI.**
If answering needs arithmetic, a scheduled job here does it and stores the
result. One exception: re-running cash flow for a *user-supplied* down payment
is fine, because the rent and monthly costs are already stored and the rest is
dividing a loan. Guessing a rent is the expensive part, and it's precomputed.

**2. "We don't know" is a real answer. Never let a blank become a number.**
No MLS feed carries nightly short-term-rental rates, so we store
`status: "unclear"` plus what to go verify (HOA minimum lease term, city STR
ordinance). Leave it empty and the AI invents a nightly rate for an investor.
And "unclear" is not "no" — saying a property *doesn't* cash-flow when we simply
don't know is a different and wrong answer.

**3. Store how many records a number came from.**
"$6,229/month" from **2 listings** and "$5,800/month" from **5 closed leases**
look identical on screen and are not the same quality of information. Every
figure carries its sample size and a confidence of good / fair / insufficient.

**4. A peak-season rate is not a monthly income figure.**
In ZIP 92211 seasonal rentals get **$10,000/month — for four months**. Across
the year that's about **$5,200**. Put the $10,000 into a mortgage calculation
and the property looks great; it isn't. That's a 2x error that reads as
completely plausible, so peak rates and annual figures are separate fields and
the docs mark which one is safe for math.

**5. When two sources disagree, keep both. Never average them.**
Landlords *ask* $6,000 for peak season; tenants actually *paid* $10,000. Both are
true — they answer different questions. Averaging to $8,000 describes nothing
real and hides the useful fact that asking prices understate this market by 67%.

**6. Write the warnings into the data, in plain sentences.**
So the AI repeats ours instead of inventing its own. Real stored example:
*"seasonal rates come from ZIP 92211, not the subdivision itself — too few lease
comps locally."* A `confidence: "fair"` code alone leaves the AI guessing what
the problem was.

**7. New analysis adds a new field; it never overwrites an old one.**
When achieved seasonal rates arrived, they went into a new `closedSeasonal`
field and left `seasonal` untouched. Overwriting would have destroyed the
comparison irreversibly, and could break a working tool.

**8. If it isn't on the cron, it's already rotting.**
Live example: `cities.subdivisionCount` says 90 for Palm Desert, which is
correct — and it was last written **2025-12-04**. Nothing refreshes it. It's
right by luck. This has bitten us twice: the seasonal layers were hand-run until
P4.6, and `cashflowProfile` until P4.7. Schedule every script, **in dependency
order**, chained with `&&` so a failed stage stops the ones that read its
output.

---

## Checklist for a new tool

1. **Write the question in a user's words.** "Does this cash-flow?" — not
   "expose cashflowStats."
2. **Pick the shape** from the table above. This decides whether you need a
   script here at all.
3. **Check `docs/tools/` first.** The data often already exists and only the
   tool is missing — there are five such gaps listed there right now.
4. **If it needs a script:** build it in `src/scripts/cma/`, with a module
   docstring that states inputs, output shape, fallback order and known limits.
   Support `--dry-run`, a single-target flag (`--slug` / `--zip` / `--city`),
   and `--all`.
5. **If it's a query or a sort:** confirm the field is populated, confirm the
   index exists, and add sanity guards against junk at the extremes.
6. **Verify against a hand calculation** on a case you know. Then re-run and
   diff to confirm you changed only what you meant to.
7. **Schedule it** in `crontab.vps`, in order, and install it live.
8. **Document it** in `docs/tools/` — shape, lookup key, real coverage count,
   reading rules.
9. **Then** the frontend builds the MCP tool against that doc.

## What makes a good MCP tool surface

- **One question, one tool.** `get_subdivision_cashflow(slug)` beats a generic
  `query(collection, field)`. The AI shouldn't need to know the schema.
- **Return the whole precomputed block** for shape 1 — it's a few KB and the
  caveats are inside it. Stripping them is how a 2-comp number gets quoted as
  fact.
- **Return as little as possible** for shapes 2 and 3. Opposite instinct, same
  goal: give the AI exactly what the question needs.
- **Pass the notes through.** `verdict.notes` and `quality.confidence` exist for
  the AI to repeat.
- **Never return a bare verdict.** "Does not cash-flow", with no break-even and
  no sample size, is a dead end for the user.
- **Say so in the tool description.** Spell out *"returns precomputed analysis —
  do not calculate this yourself."* That sentence is what stops the AI
  reasoning when it could be reading.

## Where to look next

- **`docs/tools/`** — the data contract for every precomputed block: producer,
  lookup key, exact shape, measured coverage, refresh cadence, reading rules.
  Start at `docs/tools/README.md`.
- **`docs/cma/`** — how the analytics actually work, plus
  `PDCC_CASHFLOW_SHOWCASE.md`, a complete worked answer that's a good quality
  bar for what a tool should be able to produce.
- **`docs/db/cleanup-and-migration.md`** — known data debt, including the unused
  indexes mentioned above.
