# Photos

**Producer:** `src/scripts/ingest/fetch-photos.py`
**Stored on:** `unified_listings.media[]` and the other lifecycle collections
**Served from:** `media[]` only

There is one photo store. Photos are an array on the listing document; there is
no separate photo collection and no join. `db.photos` does not exist, and
`cache-photos.py`, which used to write it, is in `archive/mls/unified/`.

## The `photoCachedAt` trap

`photoCachedAt` is stamped on the listing whenever a fetch runs, including when
that fetch fails or the feed returns nothing. A listing with no photos therefore
looks fetched, and both the default mode and `--delta` skip it forever.

`--missing-media` exists for this. It selects on `media.0` being absent and
ignores `photoCachedAt` entirely:

```
.venv/bin/python3 src/scripts/ingest/fetch-photos.py \
    --all --missing-media --collection unified_listings
```

When photos look wrong, check `media[]` on the listing and run that. Do not
trust `photoCachedAt` as evidence a listing has photos.

## Coverage floors

Two limits are the feed, not a bug, and fetching more will not move either:

- **~520 live listings have no photos in Spark at all.** The feed serves none
  for them.
- **Off-market coverage is 88.8%.** Spark strips photos from off-market
  listings, so the gap cannot be refetched.

Treat both as the floor. A coverage number at or near them means the pipeline is
working.

## The off-market archive

`/root/backups/photos-20261013.jsonl.gz` (85 MB, sha256 in
`photos-20261013.jsonl.gz.manifest.json`) is the only remaining copy of photos
for 16,493 off-market listings that Spark no longer serves. It is not a
convenience copy — if those photos are ever needed again, this file is the
source. Do not delete it.
