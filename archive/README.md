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
