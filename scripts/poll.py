import argparse

from app.db import connect, init_schema
from app.ingest import poll, sync_vehicles
from app.intangles import IntanglesClient


def main():
    parser = argparse.ArgumentParser(description="Poll recent OBD data from Intangles (for cron)")
    parser.add_argument("--vehicles", nargs="*", help="restrict to these vehicle numbers")
    args = parser.parse_args()

    init_schema()
    client = IntanglesClient()
    with connect() as conn:
        n = sync_vehicles(client, conn)
        print(f"synced {n} vehicles")
        totals = poll(client, conn)
        print(f"done: {totals['requests']} requests, {totals['records']} telemetry records, "
              f"{totals['gps_records']} GPS records")


if __name__ == "__main__":
    main()
