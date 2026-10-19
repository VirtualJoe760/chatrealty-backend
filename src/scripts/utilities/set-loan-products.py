#!/usr/bin/env python3
"""
Read or reseed the financing assumptions in `loan_products`.

Two stored documents, both read by investor/build-financing.py:

  down_payment_ladder  the rungs every listing gets priced at, 0% to 80%
  mortgage_insurance   what a low-down-payment loan costs per year, by LTV

These are lender-pricing assumptions, not law, so they are stored rather than
written into code — changing a rung or an MI rate needs no deploy. The ladder is
ONE continuous list with no eligibility gating: we are not writing the loan, we
are showing what each down payment does to the cash flow, and a reader decides
which rung applies to them. MI likewise appears as a dollar cost, never as a
rule about who may borrow.

Usage:
    python src/scripts/utilities/set-loan-products.py           # show current
    python src/scripts/utilities/set-loan-products.py --seed    # write defaults
    python src/scripts/utilities/set-loan-products.py --seed --force
                                                     # overwrite edited values

`--seed` is idempotent and will NOT clobber a document that has been edited
since it was seeded (`editedAt` newer than `seededAt`) unless given --force.
"""

import os
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

LADDER_KEY = "down_payment_ladder"
MI_KEY = "mortgage_insurance"

ESTIMATE_NOTE = (
    "Estimates, not quotes. Rates, fees and mortgage-insurance pricing vary by "
    "lender, credit profile and property, and state and local fees are not "
    "modeled. Nothing here is an eligibility determination."
)

# One continuous ladder. 0% is in it because a VA buyer really can put nothing
# down; 80% is the top because past that the loan stops driving the answer.
LADDER = [0.00, 0.03, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30,
          0.40, 0.50, 0.60, 0.75, 0.80]

# The rungs a read surface leads with, so a listing card is not 13 rows wide.
# Everything else stays stored and reachable; this is display order only.
SHOWCASE = [0.03, 0.10, 0.20, 0.50, 0.75]

# Annual mortgage-insurance cost as a fraction of the loan balance, banded by
# LTV at origination. `minLtv` is exclusive, `maxLtv` inclusive, so a rung's
# LTV (1 - down) lands in exactly one band. Ballpark borrower-paid monthly MI
# for a ~700-740 score; it is a cost estimate, not a rate sheet.
MI_BANDS = [
    {"minLtv": 0.97, "maxLtv": 1.00, "annualRate": 0.0000, "upfrontFeePct": 0.0215,
     "label": "100% LTV: no monthly MI, an upfront funding fee instead"},
    {"minLtv": 0.95, "maxLtv": 0.97, "annualRate": 0.0110, "upfrontFeePct": 0.0,
     "label": "3-5% down"},
    {"minLtv": 0.90, "maxLtv": 0.95, "annualRate": 0.0062, "upfrontFeePct": 0.0,
     "label": "5-10% down"},
    {"minLtv": 0.85, "maxLtv": 0.90, "annualRate": 0.0041, "upfrontFeePct": 0.0,
     "label": "10-15% down"},
    {"minLtv": 0.80, "maxLtv": 0.85, "annualRate": 0.0028, "upfrontFeePct": 0.0,
     "label": "15-20% down"},
    {"minLtv": 0.00, "maxLtv": 0.80, "annualRate": 0.0000, "upfrontFeePct": 0.0,
     "label": "20% down or more: no MI"},
]

# MI comes off when the amortized balance reaches this share of the original
# price. A multi-year projection that charges it for all 30 years overstates the
# cost of a low-down-payment buy, which is the whole question the ladder asks.
DROP_OFF_LTV = 0.78


def mi_for_ltv(bands, ltv):
    """The one band an LTV falls in. minLtv exclusive, maxLtv inclusive."""
    for b in bands:
        if b["minLtv"] < ltv <= b["maxLtv"]:
            return b
    return None


def validate():
    """Fail loudly here rather than silently mispricing a rung downstream."""
    if sorted(set(LADDER)) != LADDER:
        raise SystemExit("[ERROR] LADDER must be sorted and free of duplicates")
    if not all(0.0 <= r < 1.0 for r in LADDER):
        raise SystemExit("[ERROR] every ladder rung must be in [0, 1)")
    missing = [r for r in SHOWCASE if r not in LADDER]
    if missing:
        raise SystemExit(f"[ERROR] showcase rungs not on the ladder: {missing}")
    for r in LADDER:
        ltv = round(1.0 - r, 10)
        if mi_for_ltv(MI_BANDS, ltv) is None and ltv > 0:
            raise SystemExit(f"[ERROR] no MI band covers LTV {ltv:.2f} "
                             f"({r*100:.0f}% down)")


def show(coll):
    ladder = coll.find_one({"_id": LADDER_KEY})
    mi = coll.find_one({"_id": MI_KEY})

    if ladder:
        rungs = ladder.get("rungs", [])
        print(f"{LADDER_KEY}: {len(rungs)} rungs")
        print("  " + "  ".join(f"{r*100:.0f}%" for r in rungs))
        print("  showcase: " + "  ".join(f"{r*100:.0f}%"
                                         for r in ladder.get("showcase", [])))
    else:
        print(f"{LADDER_KEY}: not set — run with --seed")

    if mi:
        bands = mi.get("bands", [])
        print(f"\n{MI_KEY}: {len(bands)} bands, "
              f"drops off at {mi.get('dropOffLtv', 0)*100:.0f}% LTV")
        for b in bands:
            cost = f"{b['annualRate']*100:.2f}%/yr"
            if b.get("upfrontFeePct"):
                cost += f" + {b['upfrontFeePct']*100:.2f}% upfront"
            print(f"  LTV {b['minLtv']*100:>5.1f}-{b['maxLtv']*100:<5.1f} "
                  f"{cost:<28} {b.get('label', '')}")
    else:
        print(f"\n{MI_KEY}: not set — run with --seed")

    if ladder and mi:
        print("\nwhat each rung costs in MI:")
        for r in ladder.get("rungs", []):
            ltv = round(1.0 - r, 10)
            b = mi_for_ltv(mi.get("bands", []), ltv)
            if b is None:
                print(f"  {r*100:>3.0f}% down  LTV {ltv*100:>5.1f}%  (no band)")
                continue
            parts = []
            if b["annualRate"]:
                parts.append(f"{b['annualRate']*100:.2f}%/yr of balance")
            if b.get("upfrontFeePct"):
                parts.append(f"{b['upfrontFeePct']*100:.2f}% upfront")
            print(f"  {r*100:>3.0f}% down  LTV {ltv*100:>5.1f}%  "
                  f"{' + '.join(parts) or 'none'}")


def _edited_since_seed(doc):
    if not doc:
        return False
    edited, seeded = doc.get("editedAt"), doc.get("seededAt")
    return bool(edited and seeded and edited > seeded)


def seed(coll, force):
    now = datetime.now(timezone.utc)
    for key, payload in (
        (LADDER_KEY, {"rungs": LADDER, "showcase": SHOWCASE,
                      "note": "Every down payment we price a listing at. One "
                              "continuous ladder, no eligibility gating. "
                              + ESTIMATE_NOTE}),
        (MI_KEY, {"bands": MI_BANDS, "dropOffLtv": DROP_OFF_LTV,
                  "note": "Mortgage insurance as an annual dollar cost by LTV, "
                          "dropping off once the balance amortizes below "
                          f"{DROP_OFF_LTV*100:.0f}% of the original price. "
                          + ESTIMATE_NOTE}),
    ):
        existing = coll.find_one({"_id": key})
        if _edited_since_seed(existing) and not force:
            print(f"[SKIP] {key} was edited after it was seeded — "
                  f"pass --force to overwrite")
            continue
        payload["seededAt"] = now
        coll.update_one({"_id": key}, {"$set": payload}, upsert=True)
        print(f"[OK] seeded {key}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true",
                    help="write the default ladder and MI bands")
    ap.add_argument("--force", action="store_true",
                    help="with --seed, overwrite documents edited since seeding")
    args = ap.parse_args()

    validate()
    coll = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=20000)["admin"]["loan_products"]

    if args.seed:
        seed(coll, args.force)
        print()
    show(coll)
    if not args.seed:
        print("\nPass --seed to write the defaults, or edit the documents "
              "directly to change an assumption.")


if __name__ == "__main__":
    main()
