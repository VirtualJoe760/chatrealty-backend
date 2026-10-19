#!/usr/bin/env python3
"""
Read or change the investor-property rate spread.

`frm_30` (Freddie Mac 30-yr fixed, fetched live by investor/build-cashflow.py)
is an OWNER-OCCUPIED rate. An investment-property loan prices above it, and how
far above is a lender-pricing assumption that moves — so it is a stored value
rather than a constant in code, and changing it needs no deploy.

The new value takes effect on the next build-cashflow.py run (Tue + Fri 2 AM),
or immediately if you rerun that script by hand.

Usage:
    python src/scripts/utilities/set-investor-spread.py            # show current
    python src/scripts/utilities/set-investor-spread.py 0.75       # set to +0.75%
    python src/scripts/utilities/set-investor-spread.py 1.25       # set to +1.25%

The argument is in PERCENTAGE POINTS, because that is how lenders quote it.
0.75 means 0.75% (stored as 0.0075).
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env.local"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise SystemExit("[ERROR] MONGODB_URI is not set in .env.local")

KEY = "investor_rate_spread"
SPREAD_MIN_PCT, SPREAD_MAX_PCT = 0.0, 5.0   # beyond 5 points it is a typo


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("percent", nargs="?", type=float,
                    help="New spread in percentage points (e.g. 0.75). Omit to read.")
    args = ap.parse_args()

    coll = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=20000)["admin"]["system_config"]

    current = coll.find_one({"_id": KEY})
    if current and isinstance(current.get("spread"), (int, float)):
        print(f"current spread: +{current['spread']*100:.2f}%  "
              f"(stored {current['spread']})")
    else:
        print("current spread: not set — build-cashflow.py will seed its default")

    base = coll.find_one({"_id": "mortgage_rate"})
    if base and isinstance(base.get("rate"), (int, float)):
        print(f"last known frm_30: {base['rate']*100:.2f}% "
              f"(owner-occupied, {base.get('source', '?')})")

    if args.percent is None:
        print("\nPass a percentage to change it, e.g. "
              "`set-investor-spread.py 0.75`.")
        return

    if not (SPREAD_MIN_PCT <= args.percent <= SPREAD_MAX_PCT):
        raise SystemExit(f"[ERROR] {args.percent} is outside "
                         f"{SPREAD_MIN_PCT}–{SPREAD_MAX_PCT} percentage points. "
                         f"The argument is in points, so 0.75 means 0.75%.")

    spread = args.percent / 100.0
    coll.update_one(
        {"_id": KEY},
        {"$set": {"spread": spread,
                  "note": "Investor-property rate premium over frm_30. "
                          "Edit this field to change it; no deploy needed.",
                  "updatedAt": datetime.now(timezone.utc)}},
        upsert=True,
    )
    print(f"\nset spread to +{args.percent:.2f}% (stored {spread})")
    if base and isinstance(base.get("rate"), (int, float)):
        print(f"  effective investor rate would be "
              f"{(base['rate'] + spread)*100:.2f}%")
    print("  takes effect on the next investor/build-cashflow.py run")


if __name__ == "__main__":
    main()
