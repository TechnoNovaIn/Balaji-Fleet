import argparse
import time

from app.db import connect, init_schema
from app.ingest import poll_live_window, sync_vehicles
from app.intangles import IntanglesClient


def main():
    parser = argparse.ArgumentParser(description="Continuous live poller (today's data, repeat loop)")
    parser.add_argument("--interval", type=int, default=10, help="seconds between live passes")
    parser.add_argument("--lookback-seconds", type=int, default=900, help="UTC window to re-read each pass (must exceed pass duration)")
    parser.add_argument("--sync-interval", type=int, default=300, help="seconds between vehicle-list syncs")
    parser.add_argument("--once", action="store_true", help="run a single pass then exit (e.g. backfill a missed day)")
    args = parser.parse_args()

    init_schema()
    client = IntanglesClient()
    last_sync = 0.0
    while True:
        started = time.time()
        try:
            with connect() as conn:
                synced = None
                if started - last_sync >= args.sync_interval:
                    synced = sync_vehicles(client, conn)
                    last_sync = started
                totals = poll_live_window(client, conn, lookback_seconds=args.lookback_seconds)
            sync_msg = f"synced {synced}, " if synced is not None else ""
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {sync_msg}"
                f"{totals['requests']} requests, {totals['obd_records']} OBD, {totals['gps_records']} GPS",
                flush=True,
            )
        except Exception as exc:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pass failed: {exc!r}", flush=True)
        if args.once:
            break
        elapsed = time.time() - started
        time.sleep(max(1, args.interval - elapsed))


if __name__ == "__main__":
    main()
