# archive/

Retired code, kept rather than deleted. Nothing here is scheduled, imported, or
on a live path. It stays in git so the history and the original intent remain
readable.

Each entry keeps its original path under `archive/`, so
`archive/mls/unified/main.py` was `src/scripts/mls/backend/unified/main.py`.
Moves are done with `git mv`, so `git log --follow` still works.

## Contents

### `mls/unified/main.py` — retired 2026-10-13
The legacy daily MLS orchestrator, superseded by `run-pipeline.py` plus
dedicated crontab entries.

It chained four steps:

    unified-fetch.py → flatten.py → seed.py → update-status.py

`run-pipeline.py` is the current entry point and chains the first three. The
fourth step did not disappear — `update-status.py` is now its own crontab entry
at 7 AM, which is what lets the status pass run on its own schedule instead of
only ever running behind a fetch.

Nothing referenced it at the time it was retired: no crontab entry, no import,
no caller. The only mentions were its own `--help` text and a line in
`src/scripts/mls/backend/unified/README.md` describing it as legacy.

Kept because it is the clearest record of the original intended daily sequence,
which is useful context for the `run-nightly.py` orchestrator (P5.1).

---

## `mls/unified/cache-photos.py` — retired 2026-10-14

Wrote the `db.photos` collection: a primary-photo cache the frontend was
believed to query directly for listing-list, map and card views.

**Why it was retired.** The site serves photos from `unified_listings.media[]`
(written by `fetch-photos.py`), not from `db.photos`. The evidence was
conclusive before anything was removed:

- 67% of a 400-listing random sample had no `db.photos` doc at all, while
  98.6% of listings had `media[]`. If list views really read `db.photos`,
  two-thirds of the site would have shown no photo.
- No code anywhere read the collection — only writers (this script plus four
  older archived ones).
- The gap predated the script's 2026-08-11 failure, so `db.photos` was never
  complete in the first place.

The script also could not have worked as written: it upserts one doc per
listing on `{listingId}`, but the collection holds one doc PER PHOTO under a
unique `photoId` index (1,056,867 docs for ~86k listings), so every insert
collided with E11000.

**What happened to the data.** Nothing was lost.

1. `fetch-photos.py --missing-media` refilled `media[]` from the Spark feed for
   every live and in-escrow listing that had none — 651 listings, 12,335
   photos, 18.9 photos each on average.
2. `migrate-photos-to-media.py` then moved everything `db.photos` still held
   uniquely into `media[]` — 16,502 listings, 23,092 photos. Nearly all were
   off-market, where Spark no longer serves photos, so this was the only
   surviving copy. Those elements carry `mediaSource: "db.photos"` so a thin
   legacy gallery stays distinguishable from a real feed fetch.
3. Verified zero listings across all three lifecycle collections were left
   holding photos only in `db.photos`, then the collection was dropped.

**Restore.** A full pre-drop export is at
`/root/backups/photos-20261013.jsonl.gz` (1,056,867 docs, 81 MB, gzipped
JSONL in BSON extended JSON; sha256 and restore command in the sibling
`.manifest.json`). It was re-verified against that checksum immediately
before the drop. Written by
`src/scripts/mls/backend/unified/export-photos-collection.py`.
