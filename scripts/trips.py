"""Compute trip segmentation for recent days and store in the `trips` table.

Each vehicle-day is re-computed idempotently (DELETE+INSERT per day), so
re-running with a wider --days just adds more days. Run after backfill/live
ingest; the API can also trigger it via POST /ingest/compute-trips.

Usage:
    python -m scripts.trips --days 7
    python -m scripts.trips --days 7 --vehicles KA06AC4044,KA06AC9527
"""

import argparse
import sys

from app.trips import compute_all


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7,
                        help="how many days back to segment (default 7)")
    parser.add_argument("--vehicles", default="",
                        help="comma-separated plates to restrict to (default: all active)")
    args = parser.parse_args()

    vehicles = [v.strip() for v in args.vehicles.split(",") if v.strip()] or None
    total = compute_all(args.days, vehicles=vehicles)
    print(f"done: {total} trips stored for {args.days} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
