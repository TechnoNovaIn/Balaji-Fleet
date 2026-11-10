"""Re-resolve per-vehicle odometer source and recompute the odometer column.

Usage: python -m scripts.resolve_odometer
"""
from app.db import connect
from app.odometer import recompute_odometer, resolve_odometer_sources


def main():
    with connect() as conn:
        resolved = resolve_odometer_sources(conn)
        updated = recompute_odometer(conn)
        conn.commit()
    print(f"resolved: {resolved}")
    print(f"recomputed odometer on {updated} telemetry rows")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT odometer_source, count(*) FROM vehicles GROUP BY odometer_source ORDER BY 1")
            for row in cur.fetchall():
                print("  ", row["odometer_source"], row["count"])


if __name__ == "__main__":
    main()
