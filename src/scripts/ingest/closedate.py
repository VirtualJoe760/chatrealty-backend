#!/usr/bin/env python3
"""
The one rule for what `unified_closed_listings.closeDate` may contain.

Imported by every writer of that field — ingest/seed.py (live status router),
ingest/update-status.py (daily refresher) and historical/seed.py (closed
backfill) — so the three cannot drift. utilities/normalize-close-dates.py
reuses it to repair rows written before it existed.

Its own module, and not part of seed.py, because ingest/ and historical/ each
contain a seed.py: a cross-folder `from seed import ...` resolves to the
importing file itself.
"""

from datetime import datetime, timedelta, timezone

# A closed doc reaches the collection two ways: historical/seed.py, which has
# always parsed closeDate into a real datetime, and the live status router,
# which used to copy the Spark value through untouched. Spark sends
# "YYYY-MM-DD", so the live path wrote strings while the backfill wrote dates
# and the field ended up mixed-BSON-type — a {"closeDate": {"$gte": <datetime>}}
# range then matches only the date-typed half and silently drops the rest.
# Coerce on every write so the field has exactly one type.

# How far ahead a closeDate may legitimately sit. Seasonal leases are booked
# months before occupancy, so a future closeDate is normal for them — measured
# on 2026-10-19, 7 lease docs closed within 3 months ahead and the next
# future-dated doc was 18 months out, which is not credible for either a lease
# or a sale. Six months sits in that gap.
FUTURE_CLOSE_GRACE = timedelta(days=183)


def coerce_close_date(value):
    """Return value as a naive-UTC datetime, or None if unusable.

    None covers both unparseable values and implausible far-future dates (the
    collection carries real examples out to year 4008, which poison any
    most-recent-sale sort).
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None

    # Mongo stores UTC; keep one representation so comparisons never mix
    # offset-aware and offset-naive values.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    if dt > datetime.now(timezone.utc).replace(tzinfo=None) + FUTURE_CLOSE_GRACE:
        return None
    return dt


