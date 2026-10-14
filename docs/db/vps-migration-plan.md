# Migration plan — MongoDB and the MCP layer onto the VPS

**Status:** proposal, nothing executed. Written 2026-10-14 against measured
state.
**Scope:** two separate questions that are easy to conflate —
(1) where the **database** runs, and (2) where the **MCP tool layer** runs. They
have different answers and should move in that order.

---

## The short version

| Question | Recommendation |
|---|---|
| Move Mongo to this VPS? | **Yes, but not onto this box as it is sized today.** Resize to ≥ 8 GB RAM first (16 GB if the frontend moves too). On 3 GB it will thrash. |
| Move the MCP tools to the VPS? | **Yes — and this is the part worth doing first.** It is low-risk, reversible, and pays off immediately. |
| Keep the frontend on Vercel? | **Yes.** Your instinct — "two repositories, and just the MCP info comes here, none of the frontend code" — is the right architecture. |
| Order | MCP layer → VPS resize → DB trim → DB migration. Never the DB first. |

The reason to do MCP before the database: an MCP server is a stateless process
you can run in two places at once and switch between in a config file. A
database is the opposite. Get the cheap, reversible win first.

---

## 1 · Measured current state

Cluster, read 2026-10-14 via `dbStats` / `collStats`:

| | |
|---|---:|
| Database | `admin` (all app data) |
| Collections | 96 |
| Documents | 1,773,390 |
| Data size (uncompressed) | 16,341 MB |
| **Storage size (on disk, compressed)** | **7,771 MB** |
| **Index size** | **1,683 MB** (677 indexes) |
| **Total on disk** | **≈ 9.5 GB** |
| Replica set | `jpsrealtor-mongodb`, **1 member** |
| Host | DigitalOcean managed (`doadmin`) |

Largest collections:

| Collection | Docs | Storage | Index | Idx # |
|---|---:|---:|---:|---:|
| `unified_closed_listings` | 1,112,910 | 3,898 MB | 1,307 MB | 46 |
| `unified_listings` | 86,124 | 1,250 MB | 248 MB | 64 |
| `unified_off_market` | 101,310 | 1,240 MB | 27 MB | 11 |
| `crmls_listings` | 136,863 | 522 MB | 45 MB | 11 |
| `crmlsClosedListings` | 94,226 | 236 MB | 2 MB | 1 |
| `unified_in_escrow` | 18,917 | 196 MB | 11 MB | 11 |
| `gpsClosedListings` | 56,029 | 152 MB | 1 MB | 1 |

This VPS, for comparison:

| | |
|---|---:|
| RAM | **3 GB** (≈ 2 GB available) |
| vCPU | 2 |
| Disk free | 68 GB of 77 GB |

---

## 2 · Why 3 GB is the blocker

Disk is not the problem — 9.5 GB into 68 GB is comfortable, even with a few
years of growth. **Memory is the problem.**

WiredTiger sizes its cache at roughly 50% of (RAM − 1 GB), so on a 3 GB box
Mongo gets about **1 GB of cache**. Against that:

- `unified_closed_listings` **indexes alone are 1,307 MB** — they do not fit in
  cache on their own, before a single document is read.
- Total index size is 1,683 MB, i.e. **1.6× the whole cache**.
- The Python cron jobs, and the MCP server, want memory on the same box.

The failure mode is not a crash, it is everything getting slow at once: index
pages evict each other, every query goes to disk, and the twice-weekly CMA jobs
that currently run in minutes start running in hours. The agent builder's closed
scan is a good example of the shape of work involved — it reads 1.1M documents
in one pass and relies on the server having room to group them.

**Sizing guidance**

| Target | Verdict |
|---|---|
| 3 GB (today) | Do not migrate. |
| 8 GB | Workable for Mongo + cron. ~3.5 GB cache holds all indexes with room for hot documents. |
| 16 GB | Comfortable, and the only option worth considering if the Next.js app also moves. |

---

## 3 · Trim before you move (≈ 1.1 GB, zero new risk)

Moving less data makes every later step faster, and this is worth doing whether
or not the migration happens. `docs/db/current-state.md` already identified
these as duplicates of `unified_*`; the sizes below are current.

| Collection | Reclaims | Why it is safe |
|---|---:|---|
| `crmls_listings` | 567 MB | Duplicates `unified_listings` + `unified_off_market` for CRMLS |
| `crmlsClosedListings` | 238 MB | Strict subset of `unified_closed_listings` |
| `gpsClosedListings` | 153 MB | Strict subset of `unified_closed_listings` |
| `closed_listings` | 84 MB | Older GPS staging |
| `listings` | 56 MB | Legacy ungrouped, `mlsSource: null` |
| `unifiedlistings_deprecated_2025_12_24` | 13 MB | Deprecated by name |
| `temp_flattened` | 9 MB | Scratch from a one-off flatten run |

**Confirm there are no readers first.** The frontend repo is not on this box, so
grep it for each name before dropping anything. Two of these were still being
written to as recently as 2026-04-28, which means something still points at
them.

There is a second, larger prize in the **index bloat**:
`unified_closed_listings` carries 46 indexes totalling 1,307 MB, and
`docs/db/current-state.md` documents the overlap in detail — five indexes whose
key starts with `city`, six starting with `subdivisionName`. Consolidating those
plausibly reclaims 400–600 MB *and* directly shrinks the cache pressure from
§2, which makes it the single highest-value piece of work on this page. Do it
with `$indexStats` to prove a given index is unused before dropping it.

> Note: a few indexes on these collections are load-bearing in non-obvious
> ways. `closeDate_ttl_5years` is a **real TTL index** (expireAfterSeconds
> 157,680,000) and is what keeps closed history at 5 years. Dropping it would
> silently let the collection grow without bound.

---

## 4 · Where the MCP layer should run

Today the production MCP tools are TypeScript in the frontend repo on Vercel,
and the OAuth state already lives in Mongo (`mcpoauthclients`, `mcpoauthcodes`,
`mcpoauthtokens`). This repo now also has a Python MCP server
(`src/mcp/agents_server.py`) which runs over stdio for Claude Code and can serve
`streamable-http` with one flag.

| | MCP on Vercel (today) | MCP on the VPS |
|---|---|---|
| Latency to data | Vercel → DO managed, every call | localhost after migration |
| Egress cost | Billed per call | None |
| Long/heavy queries | Bounded by function timeout | Unbounded |
| Scaling | Automatic | You own it |
| Uptime | Vercel's | **One box, single point of failure** |
| TLS / OAuth | Handled by the platform | You run Caddy/nginx + reuse `mcpoauth*` |
| Code sharing with builders | None — reimplemented in TS | **Direct: same Python, same helpers** |

**Recommendation: the split you described.** Frontend code stays on Vercel. The
MCP tool layer moves to the VPS and keeps no UI. The deciding factor is the last
row: right now a tool's query logic is written twice, once in a Python builder
and once in a TypeScript tool, and the two drift. The `closeDate` mixed-type
trap in §5 is exactly the kind of bug that gets fixed in one language and not
the other.

Two things to accept before committing: the VPS becomes a single point of
failure for production chat, and you take on TLS, process supervision and
OAuth. If that is not acceptable, a middle path is to keep a thin Vercel route
that proxies to the VPS — you get code consolidation without exposing the box
directly, at the cost of the hop you were trying to remove.

---

## 5 · Data problems to fix before or during the move

These are real and already affect production reads. Found while building the
agent database on 2026-10-14.

**`closeDate` is mixed BSON types.** On `unified_closed_listings`: 1,028,003
`date`, **84,903 `string`**, 4 missing. Two live consequences:

- `{closeDate: {$gte: <datetime>}}` matches **only** the date-typed documents
  and silently drops all 84,903 strings. Those strings run to 2026-10-12 — they
  are current data, not old junk.
- Conversely `{closeDate: {$gte: "2021-10-14"}}` drops all 1,028,003
  date-typed documents.
- The TTL index can only expire date-typed values, so the string-typed rows are
  exempt and reach back to 2020-08-28, past the nominal 5-year horizon.

Both query shapes exist in the current CMA builders:
`build-pool-premium.py:257`, `build-rent-rates.py:422` and
`build-closed-seasonal-rates.py:512` compare against datetimes, while
`build-listing-cma.py:534` compares against an `.isoformat()` string. Each is
losing a slice of the collection. The builders are right to normalize in Python
afterwards (`coerce_date`), but by then the `$match` has already dropped rows.

*Fix:* normalize the field itself to BSON date in a one-off migration, then keep
it normalized at ingest in `unified-fetch.py`. A migration is strictly better
than teaching every reader to use `$convert`, because the converted form cannot
use an index. **Do this while still on managed Mongo** — it is a data fix, not a
migration step, and it is much easier to reason about without a cutover in
flight.

**35 corrupt future `closeDate` values**, the worst being year 5015. They poison
any "most recent sale" sort. Clamp at ingest and repair the existing rows.

**Single-member replica set, and no backups.** Today's cluster is one node, and
`project_jpsrealtor_vps_data_gaps` records that there are no DB backups and that
a dev-PC sync shares this cluster. Both facts make every step here riskier than
it looks, and both must be resolved before a cutover — see §6 step 0.

---

## 6 · The migration, step by step

**Step 0 — earn the right to start.** Non-negotiable, in order:

1. Stand up real backups: `mongodump` on a schedule, restored at least once to
   prove the dump is good. An untested dump is not a backup.
2. Settle the shared dev-PC sync. Two writers against one cluster during a
   cutover is how you lose data silently.
3. Resize the VPS per §2.
4. Fix `closeDate` per §5.

**Step 1 — MCP layer to the VPS** (reversible in one config file)
- Run `agents_server.py --transport streamable-http` under systemd; `ops/systemd`
  already exists as a home for the unit.
- Terminate TLS with Caddy or nginx; reuse the `mcpoauth*` collections.
- Point one non-critical tool at it, in parallel with Vercel. Compare responses.
- Migrate tools across one at a time. Roll back by repointing the client.

**Step 2 — install Mongo on the VPS**
- Match the major version exactly: **8.0** (cluster reports 8.0.26, FCV 8.0).
- Configure a replica set **even with one member**, so you have an oplog and the
  option of adding a secondary later. The current setup already runs this way.
- Set `wiredTigerCacheSizeGB` explicitly rather than taking the default; leave
  headroom for the cron jobs.

**Step 3 — seed, then catch up**
- `mongodump` from managed → `mongorestore` to the VPS. At ~9.5 GB this is
  minutes of transfer, not hours.
- Keep writes on managed while you verify.
- Catch up the delta. The honest options: a short write freeze (simplest, and
  this is a twice-weekly-cron workload that tolerates one), or `mongosync` /
  oplog tailing if downtime is unacceptable. **Prefer the freeze** — the jobs
  run at 1–2 AM and nothing user-facing writes continuously.

**Step 4 — verify before cutting over**
- Document counts per collection, both sides.
- Index inventory per collection — a restore can silently skip an index build.
- Spot-check the precomputed blocks (`cashflowProfile`, `cmaStats`,
  `rentStats`, and the new `agents` / `brokerages`).
- Re-run one builder in `--dry-run` against the VPS copy.

**Step 5 — cut over**
- Change `MONGODB_URI` in `.env.local` here and in the frontend's environment.
- Nothing hardcodes a database name — `how-to-call.md` already insists on
  `client.get_database()` / the URI default. This is what makes the cutover a
  one-line change, so keep it that way.
- Watch the first scheduled cron chain end to end.

**Step 6 — keep the old cluster running, read-only, for a week.** It is the
rollback. Do not destroy it on day one.

---

## 7 · Rollback

Up to and including step 5, rollback is: put the old `MONGODB_URI` back. That
holds as long as the managed cluster is still alive and you have not taken
writes on the VPS that matter. After the first real write lands on the VPS, you
are reconciling two datasets by hand — which is the reason for the write freeze
in step 3 rather than a live dual-write.

---

## 8 · What this buys, honestly

**Gains:** no managed-database bill; no network hop or egress for every query;
localhost-speed reads for the MCP layer; one language for query logic; freedom
to run long jobs without a serverless timeout.

**Costs:** you become the DBA. Backups, upgrades, disk monitoring, TLS and
uptime all become yours, and production chat gains a single point of failure
that it does not have today.

For a product at this stage that is usually a good trade — but only after step 0
is genuinely done. The current combination of one replica-set member, no
backups, and a second writer on the same cluster is a worse risk than the
managed bill, and migrating would lock it in rather than fix it.
