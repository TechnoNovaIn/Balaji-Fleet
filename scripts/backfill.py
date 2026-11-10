import argparse

from app.db import connect, init_schema
from app.ingest import backfill, sync_vehicles
from app.intangles import IntanglesClient


def main():
    parser = argparse.ArgumentParser(description="Backfill historical OBD data from Intangles")
    parser.add_argument("--days", type=int, default=365, help="number of days to backfill (default 365 = full year)")
    parser.add_argument("--vehicles", nargs="*", help="restrict to these vehicle numbers")
    parser.add_argument("--include-today", action="store_true", help="also backfill today (recovery when the live poller was down)")
    args = parser.parse_args()

    init_schema()
    client = IntanglesClient()
    with connect() as conn:
        n = sync_vehicles(client, conn)
        print(f"synced {n} vehicles")
        totals = backfill(client, conn, args.days, args.vehicles, include_today=args.include_today)
        print(f"done: {totals['requests']} requests, {totals['records']} telemetry records, "
              f"{totals['gps_records']} GPS records")


if __name__ == "__main__":
    main()
