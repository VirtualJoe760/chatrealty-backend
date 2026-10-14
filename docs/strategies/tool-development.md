# Strategy — Tool Development

**Audience:** anyone adding an AI-facing capability to chatrealty, on either side of
the repo split. Read this before writing a builder script *or* an MCP tool.

**The one-line version:** the model should never calculate. It should look up a
number a Python job already calculated, and say where that number came from.

---

## 1 · The problem this solves

An LLM asked "would this Palm Desert condo cash-flow as a rental?" will, left to
itself, try to *do the work*: pull comps, pull lease listings, guess a rent,
guess taxes and insurance, amortize a loan, and narrate the arithmetic. That
approach fails four ways at once.

| Failure | Why it happens |
|---|---|
| **Cost** | Tens of thousands of tokens per answer, most of it raw listing JSON the user never sees. Multiply by every session. |
| **Latency** | Several tool round-trips plus the model's own reasoning. A question that should take 200 ms takes 30 s. |
| **Inconsistency** | The same question asked twice gets two different rents, because nothing pinned the comp set or the assumptions. |
| **Unauditability** | When a number is wrong you cannot tell whether the data, the comp selection, or the model's mental math was at fault. |

Worse, the model is *confidently* wrong in a domain where being wrong has
consequences. A fabricated rent estimate presented to an investor is not a
rounding error.

## 2 · The pattern: precompute, then serve

Split every capability into two halves that meet at a stored document.

```
   ┌─ BACKEND (this repo, Python, cron) ──────────────────────────────┐
   │  MLS feed → unified_listings / unified_closed_listings           │
   │       ↓                                                          │
   │  builder script: does ALL the analysis, once, offline             │
   │       ↓                                                          │
   │  writes a self-describing block onto a document                  │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │   MongoDB
   ┌──────────────────────────────┴───────────────────────────────────┐
   │  MCP tool: ONE findOne / ONE indexed find. No math.              │
   │       ↓                                                          │
   │  model receives a small, complete, pre-reasoned answer           │
   └──────────────────────────────────────────────────────────────────┘
```

The stored block is the contract. Everything the model needs to answer *and
caveat* the question lives in it — the figures, the sample sizes, the
provenance, the confidence, and plain-language notes about what is shaky.

### What this buys

- A cash-flow answer for a whole subdivision is **one `findOne` by slug**.
  Three rental strategies, per-bedroom breakdowns, rent provenance, and the
  caveats come back in a few KB.
- The answer is **identical** every time until the next cron run, and the
  `lastUpdated` field says when that was.
- A number can be traced to the script that produced it and the sample it came
  from. `build-cashflow-profile.py` computes no economics of its own — it is
  medians over per-listing `cashflowStats` — so a rollup can never disagree with
  the listing detail a user drills into.

## 3 · Non-negotiable rules

These exist because each one has already cost us a real bug.

**1. Never compute at request time.** If an MCP tool needs arithmetic beyond
reading a field, the arithmetic belongs in a builder. The sole exception:
re-deriving cash flow for a *user-supplied* down payment or rate, which is
trivial arithmetic over stored `fixedCosts` — that is why `fixedCosts` is
stored. Rent estimation is the expensive part and it is precomputed.

**2. A missing signal is `unclear`, never a number, and never `negative`.**
Those are different answers to an investor. Short-term nightly rates do not
exist in any MLS feed, so `verdict.shortTerm.status` is permanently `unclear`
with a reason telling the user what to verify. Modelling it would be invention.

**3. Sample size travels with every estimate.** A rate built on 2 comps and a
rate built on 69 closings must not look alike to the consumer. Every block
carries `sampleSize` and a `quality.confidence` of `good` / `fair` /
`insufficient`.

**4. Respect unit discipline — it is the easiest way to be wrong by 2x.** A
seasonal *peak* rate is not a monthly income figure. `seasonalRate` ($10,000 in
ZIP 92211) describes four peak months; feeding it into a payment calculation
overstates annual income roughly 2x. Only the `annualizedMonthly*` fields may
touch cash-flow math. Builders state this in their docstrings; tools must honor
it.

**5. Independent sources stay separate. Never silently blend.** We have two
seasonal rent layers: what tiered listings *ask* and what leases *closed at*.
In ZIP 92211 they disagree by 1.67x ($6,000 vs $10,000) and **both are
correct** — they answer different questions. They are stored as `seasonal` and
`closedSeasonal` and scored as separate strategies. Averaging them would
destroy the only signal worth having. Where a comparison is useful, compute it
once and store it (`rents.seasonalDivergence`) rather than leaving every
consumer to re-derive it and risk comparing an asking peak against an achieved
blend.

**6. Say the awkward thing in the data.** When a figure is weak, the block says
so in `quality.notes` / `verdict.notes`, in language a user can read. Real
examples now shipping: *"seasonal rates come from ZIP 92211, not the
subdivision itself — too few tiered lease comps locally"* and *"the seasonal
rate rests on asking prices only; no closed seasonal lease in this area
corroborates it."* The model should surface these, not hide them.

**7. New layers are additive.** A new builder writes a new block; it does not
edit another builder's output. That keeps blast radius at one field and lets two
layers be compared instead of one overwriting the other.

**8. Schedule it or it rots.** An unscheduled builder is a one-off snapshot that
silently drifts from the data beneath it. This has bitten us twice: the seasonal
layers went stale before P4.6 scheduled them, and `cashflowProfile` — the single
document the MCP tools serve — was hand-run only until P4.7. Add a stage to a
dependency chain and it must be chained in order with `&&` so nothing reads a
half-built input.

## 4 · Lifecycle of a new tool

1. **Name the question** in a user's words. "Does this cash-flow as a rental?"
   Not "expose cashflowStats."
2. **Find the gap.** What does answering it require that is not already stored?
   Check `docs/tools/` first — often the data exists and only the tool is
   missing.
3. **Write the builder** in `src/scripts/cma/`. Its module docstring is the
   spec: inputs, output shape, fallback order, known limitations, usage. Support
   `--dry-run`, a single-target flag (`--slug` / `--zip` / `--city`), and
   `--all`.
4. **Be honest in the output.** Confidence tiers, sample sizes, `source` fields
   recording which fallback fired, and notes for anything a user would want
   flagged.
5. **Verify against a hand calculation** on a known case before trusting it.
   Then re-run and diff against the previous output to confirm you changed only
   what you meant to.
6. **Schedule it** in `crontab.vps`, in dependency order, and install it live.
7. **Document it** in `docs/tools/` with the shape, the lookup key, the real
   coverage count, and the reading rules.
8. **Then** the frontend builds the MCP tool against that doc.

## 5 · What makes a good MCP tool surface

- **One question, one tool.** `get_subdivision_cashflow(slug)` beats a generic
  `query_profile(collection, field)`. The model should not have to know the
  schema.
- **Return the whole block.** It is a few KB and it carries the caveats. Do not
  cherry-pick fields and strip the provenance — that is how a 2-comp number gets
  quoted as fact.
- **Pass through the notes.** `verdict.notes` and `quality.confidence` exist for
  the model to repeat.
- **Never return a bare verdict.** "Does not cash-flow" with no break-even
  figure, no strategy breakdown, and no sample size is a dead end for the user.
  The block already contains the useful version; send it.
- **Describe the tool so the model reaches for it instead of reasoning.** The
  tool description should say *"returns precomputed cash-flow analysis — do not
  calculate this yourself"* in as many words.

## 6 · Reading guide

`docs/tools/README.md` indexes every precomputed block: producer script, lookup
key, coverage, refresh cadence, and reading rules. Start there.

Deep background on the analytics themselves lives in `docs/cma/`:
`RENTAL_CASHFLOW.md` (the rent + cash-flow stack), `SUBDIVISION_AND_CITY_CMA.md`
and `LISTING_CMA.md` (sale comps), and `PDCC_CASHFLOW_SHOWCASE.md` (a complete
worked answer, useful as a target for what a tool should be able to produce).
