"""Ingest a vehicle spec sheet (VIN, kerb, payload capacity, GVW) into the vehicles table.

Usage:
    python -m scripts.ingest_specs data/vehicle_specs.csv [--dry-run]

CSV columns (header required):
    vehicle_no  — plate (spaces stripped) or VIN; primary join key
    gvw         — gross vehicle weight (kg)
    unladen     — unladen / self-weight of the lorry (kg)
    payload_capacity — payload capacity (kg)
    make_model  — optional; stored as kerb_model for family lookup

Matching:
    1. vehicle_no (plate, spaces stripped) against vehicles.vehicle_no
    2. Fallback: chassis_no (VIN) against vehicles.vehicle_no (VIN aliases)

Sets: gvw_kg, payload_capacity_kg, kerb_weight_kg = unladen, kerb_model = make_model.
"""

import argparse
import csv
import sys

from app.db import connect


def normalize_plate(p):
    return p.strip().upper().replace(" ", "").replace("-", "")


def run(path, dry_run=False):
    rows = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT vehicle_no, intangle_id FROM vehicles")
            all_vehicles = cur.fetchall()
            plate_to_id = {r["vehicle_no"]: r["intangle_id"] for r in all_vehicles}
            vin_to_id = {}
            for r in all_vehicles:
                if r["vehicle_no"] and len(r["vehicle_no"]) > 10:
                    vin_to_id[r["vehicle_no"]] = r["intangle_id"]

            matched = 0
            unmatched_plates = []
            unmatched_vin = []
            skipped = 0

            for r in rows:
                plate_raw = r.get("vehicle_no", "")
                plate = normalize_plate(plate_raw)
                gvw = r.get("gvw")
                unladen = r.get("unladen")
                payload = r.get("payload_capacity")
                make_model = r.get("make_model")

                if not plate:
                    continue

                vehicle_id = plate_to_id.get(plate)
                if vehicle_id is None:
                    vid = vin_to_id.get(plate)
                    if vid is not None:
                        vehicle_id = vid

                if vehicle_id is None:
                    unmatched_plates.append(plate_raw)
                    continue

                try:
                    gvw_val = float(gvw) if gvw else None
                    unladen_val = float(unladen) if unladen else None
                    payload_val = float(payload) if payload else None
                except (ValueError, TypeError):
                    skipped += 1
                    continue

                if dry_run:
                    matched += 1
                    print(f"DRY-RUN: {plate} -> intangle_id={vehicle_id} "
                          f"gvw={gvw_val} unladen={unladen_val} payload={payload_val} model={make_model}")
                else:
                    cur.execute(
                        """
                        UPDATE vehicles
                        SET gvw_kg = %s, payload_capacity_kg = %s,
                            kerb_weight_kg = %s, kerb_model = %s
                        WHERE intangle_id = %s
                        """,
                        (gvw_val, payload_val, unladen_val, make_model, vehicle_id),
                    )
                    matched += 1

            if not dry_run:
                conn.commit()

    print(f"{'DRY-RUN: ' if dry_run else ''}matched={matched} "
          f"unmatched_plates={len(unmatched_plates)} unmatched_vin={len(unmatched_vin)} "
          f"skipped={skipped}")
    if unmatched_plates:
        print("Unmatched plates:", ", ".join(unmatched_plates[:20]))
    if unmatched_vin:
        print("Unmatched VINs:", ", ".join(unmatched_vin[:20]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.csv_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()