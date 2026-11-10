"""Continuous trip recompute loop: keeps the `trips` table current so the
Timetable always shows trip start/end markers and per-trip load labels.

Runs forever in the background. Each pass recomputes trips idempotently
(DELETE+INSERT per vehicle-day) for the last N days, so the timetable picks up
new trips + fresh payload-regressor labels automatically.

Usage:
    python -m scripts.trips_loop --interval-minutes 15 --days 3
"""

import argparse
import time

from scripts.trips import compute_all


def main():
    parser = argparse.ArgumentParser(description="Continuous trips recompute loop")
    parser.add_argument("--interval-minutes", type=float, default=15,
                        help="minutes between recompute passes")
    parser.add_argument("--days", type=int, default=3,
                        help="how many days back to segment each pass (incl. today)")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass then exit")
    args = parser.parse_args()

    while True:
        started = time.time()
        try:
            total = compute_all(args.days)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] trips: {total} stored "
                  f"for {args.days} days", flush=True)
        except Exception as exc:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] trips pass failed: {exc!r}",
                  flush=True)
        if args.once:
            break
        elapsed = time.time() - started
        time.sleep(max(1, args.interval_minutes * 60 - elapsed))


if __name__ == "__main__":
    main()
