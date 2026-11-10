"""Ingest shipment manifest data (ground-truth net weights) into the `shipments` table.

Source: data/shipments.csv (vehicle, shipment_date, net_weight_t, ...). Idempotent
(ON CONFLICT DO NOTHING on shipment_id). Re-run safely after a refresh.
"""

import argparse
import csv

from app.db import connect, init_schema

CSV_PATH = "data/shipments.csv"


def ingest_shipments(path=CSV_PATH):
    init_schema()
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                wt = float(r["net_weight_t"]) if r.get("net_weight_t") else None
            except ValueError:
                wt = None
            rows.append({
                "shipment_id": int(r["shipment_id"]),
                "vehicle_no": "".join(r["vehicle"].split()).upper(),
                "shipment_type": r.get("shipment_type"),
                "net_weight_t": wt,
                "shipment_date": r["shipment_date"],
                "party": r.get("party"),
                "vehicle_type": r.get("vehicle_type"),
            })
    inserted = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO shipments (shipment_id, vehicle_no, shipment_type,
                                           net_weight_t, shipment_date, party, vehicle_type)
                    VALUES (%(shipment_id)s, %(vehicle_no)s, %(shipment_type)s,
                            %(net_weight_t)s, %(shipment_date)s, %(party)s, %(vehicle_type)s)
                    ON CONFLICT (shipment_id) DO NOTHING
                    """,
                    r,
                )
                inserted += cur.rowcount
        conn.commit()
    print(f"shipments: {len(rows)} rows in file, {inserted} new inserted")
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=CSV_PATH)
    args = parser.parse_args()
    ingest_shipments(args.path)
