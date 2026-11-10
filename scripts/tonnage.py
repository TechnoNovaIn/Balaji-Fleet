"""Compute physics payload estimates for the fleet and cache them on vehicles.

Usage:
    python -m scripts.tonnage [--days N] [--vehicles A,B] [--limit N]

Scans vehicles with telemetry in the lookback window, runs the physics solver
per vehicle (energy + accel + cruise estimators, self-calibrated tare) and
writes payload_tonnes / total_mass_kg / tare_mass_kg / tonnage_note /
tonnage_updated_at into the vehicles table. Idempotent; safe to re-run.

When trained artifacts exist (models/), the learned per-family calibration
factor replaces the blanket 2.0 and the loaded/empty LightGBM classifier finds
empty windows for tare on trucks that emit no load flag.
"""

import argparse
import sys

from app.db import connect
from app.tonnage import estimate_trip
from app.ml_tonnage import load_calibration, load_classifier, calibration_factor_for


def run(days=7, vehicles=None, limit=0):
    calibration = load_calibration()
    classifier = load_classifier()
    with connect() as conn:
        with conn.cursor() as cur:
            if vehicles:
                cur.execute(
                    "SELECT DISTINCT vehicle_no FROM vehicles WHERE vehicle_no = ANY(%s)",
                    (vehicles,),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT v.vehicle_no
                    FROM vehicles v
                    JOIN telemetry t ON t.vehicle_id = v.intangle_id
                    WHERE v.is_active AND t.time > now() - make_interval(days => %s)
                    ORDER BY v.vehicle_no
                    """,
                    (days,),
                )
            plates = [r["vehicle_no"] for r in cur.fetchall()]
            if limit:
                plates = plates[:limit]

        done = 0
        skipped = 0
        kerb_fallback = 0
        for plate in plates:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT time, speed, rpm, gear, fuel_rate, engine_load, accel_pedal,
                           vehicle_load_cond, v.kerb_weight_kg, v.kerb_model,
                           v.trailer_weight_kg, v.vehicle_type
                    FROM telemetry t
                    JOIN vehicles v ON v.intangle_id = t.vehicle_id
                    WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
                      AND t.speed > 2 AND t.rpm > 0 AND t.fuel_rate > 0
                    ORDER BY t.time
                    """,
                    (plate, days),
                )
                rows = cur.fetchall()
            if len(rows) < 30:
                skipped += 1
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE vehicles SET tonnage_note = %s, tonnage_updated_at = now() "
                        "WHERE vehicle_no = %s",
                        ("insufficient telemetry", plate),
                    )
                continue
            meta = rows[0] if rows else {}
            est = estimate_trip(
                [dict(r) for r in rows],
                vehicle_no=plate,
                family="blazo_x48t",
                kerb_weight=meta.get("kerb_weight_kg"),
                kerb_model=meta.get("kerb_model"),
                trailer_weight_kg=meta.get("trailer_weight_kg"),
                vehicle_type=meta.get("vehicle_type"),
                calibration_factor=calibration_factor_for(meta.get("vehicle_type"), calibration),
                classifier=classifier,
            )
            if est.tare_source == "kerb":
                kerb_fallback += 1
            note = est.note if est.note else "ok"
            payload = est.payload_tonnes
            if est.total_mass_kg is None:
                note = "insufficient data"
                payload = None
            elif est.tare_mass_kg and est.tare_mass_kg >= est.total_mass_kg:
                note = "grade-limited (total below tare)"
                payload = None
            elif est.total_mass_kg < 4000:
                note = "grade-limited (implausibly low mass)"
                payload = None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE vehicles
                    SET payload_tonnes = %s, total_mass_kg = %s, tare_mass_kg = %s,
                        tonnage_note = %s, tonnage_updated_at = now()
                    WHERE vehicle_no = %s
                    """,
                    (
                        payload,
                        est.total_mass_kg,
                        est.tare_mass_kg,
                        note,
                        plate,
                    ),
                )
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(plates)} computed, last {plate}")
        conn.commit()
        print(f"done: {done} computed, {skipped} skipped (insufficient telemetry), "
              f"{kerb_fallback} kerb-fallback (no self-calibrated tare), total {len(plates)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--vehicles", type=str, default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    vehicles = [v.strip() for v in args.vehicles.split(",") if v.strip()] or None
    try:
        run(days=args.days, vehicles=vehicles, limit=args.limit)
    except KeyboardInterrupt:
        print("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
