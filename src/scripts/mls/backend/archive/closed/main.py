import subprocess
import sys
from pathlib import Path
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
CRMLS_DIR = BASE_DIR / "crmls"
GPS_DIR = BASE_DIR / "gps"

# CRMLS scripts run first (more closed listings), then GPS
SCRIPT_PIPELINES = [
    ("CRMLS", CRMLS_DIR, [
        "fetch.py",
        "flatten.py",
        "seed.py",
    ]),
    ("GPS", GPS_DIR, [
        "fetch.py",
        "flatten.py",
        "seed.py",
    ]),
]

# ──────────────────────────────────────────────────────────────────────────────
# MAIN - ONE-TIME HISTORICAL CLOSED LISTINGS PULL
# ──────────────────────────────────────────────────────────────────────────────

def main():
    start_time = datetime.now()
    print("\n" + "="*80)
    print("🏠  JPS Realtor - CLOSED LISTINGS Historical Pull (For Comps)")
    print("="*80)
    print(f"🕐 Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📝 This will fetch ALL closed/sold listings from both MLS systems")
    print(f"⏳ This is a large operation and may take several hours to complete")
    print("="*80 + "\n")

    for mls_name, working_dir, scripts in SCRIPT_PIPELINES:
        print(f"\n{'='*80}")
        print(f"🏢  Starting {mls_name} CLOSED Listings Pipeline")
        print(f"{'='*80}\n")

        for script in scripts:
            path = working_dir / script
            print(f"➡️  Running {mls_name}/{script}...")
            result = subprocess.run([sys.executable, str(path)], cwd=working_dir)
            if result.returncode != 0:
                print(f"❌ {mls_name}/{script} failed (exit code {result.returncode}). Stopping pipeline.")
                print(f"\n⏱️  Total time before failure: {datetime.now() - start_time}")
                sys.exit(result.returncode)
            print(f"✅ {mls_name}/{script} completed successfully.\n")

        print(f"✅ {mls_name} closed listings pipeline completed successfully.\n")

    end_time = datetime.now()
    duration = end_time - start_time

    print("\n" + "="*80)
    print("🎉 All CLOSED LISTINGS pipelines finished successfully!")
    print("="*80)
    print(f"🕐 Started:  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🕐 Finished: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏱️  Duration: {duration}")
    print(f"📊 Data saved to MongoDB collections:")
    print(f"   - crmlsClosedListings (CRMLS sold properties)")
    print(f"   - gpsClosedListings (GPS sold properties)")
    print(f"🎯 These collections can now be used for generating comps!")
    print("="*80 + "\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Pipeline interrupted by user. Exiting gracefully...")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Unexpected error in closed listings pipeline: {e}")
        sys.exit(1)
