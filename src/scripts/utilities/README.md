# Utilities

Hand-run tools and spent one-shots. Nothing here is on cron.

```
utilities/
├── purge-stale.py                  safety-guarded stale-listing purge
├── count.py                        collection counts
├── clean.py, clean_collections.py  collection cleanup
├── patch.py                        one-shot field patch from a flattened dump
├── extract_subdivisions.py         pull subdivision names out of listings
├── subdivision_map.py              subdivision name mapping
├── fetch_one.py, test_fetch.py     single-listing Spark fetch probes
├── export-photos-collection.py     spent: exported the retired photo store
├── migrate-photos-to-media.py      spent: moved that store into media[]
└── normalize-close-dates.py        spent: made closeDate a single BSON type
```

`normalize-close-dates.py` is the exception to "spent": it is dry-run by
default and reports the collection's `closeDate` type mix, so it doubles as the
check that no writer has reintroduced a string. `ingest/closedate.py` holds the
rule it enforces.

**Read a script before running it.** Only `purge-stale.py` is safety-guarded;
the rest are unguarded, most have no docstring, and several are one-shots whose
input files no longer exist. Several write to the database directly.
