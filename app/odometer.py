"""Odometer source resolution.

The OBD API reports odometer under several PID keys (245, EMS_245,
mhn_blazo_245) that can disagree per vehicle. Each vehicle is assigned a
trusted source and the odometer column is derived from it.

Verified fleet behaviour (2026-07-31):
- mhn_blazo_245 always equals PID 245 (Blazo firmware overwrite).
- PID 245 is the standard J1939 odometer; EMS_245 agrees on ~74% of
  vehicles, drifts ~4% on many, and is badly scaled on a handful.
- On 5 vehicles PID 245 shows odometer-reset drops (5-10k km) while
  EMS_245 stays clean; those get EMS_245 as source.
- On a few more, PID 245 is continuous but SCALED WRONG relative to EMS_245
  (e.g. a doubled register, or a ~6x larger bogus counter). The raw-245 drop
  heuristic misses those, so we ALSO compare the two registers' medians when
  both are present: if they disagree by > 25%, the ECM register (EMS_245) is
  trusted. Mixing two disagreeing registers in one column breaks trip distance,
  so consistency beats guesswork.
"""

ODO_ALIASES = {
    "245": "odometer_245",
    "EMS_245": "odometer_ems",
    "mhn_blazo_245": "odometer_mhn",
}

DROP_THRESHOLD_KM = 1000.0

# Shared with ingest.sync_vehicles: serializes vehicle/odometer writes so
# concurrent pollers (live + backfill) can't deadlock on the vehicles table.
SYNC_LOCK_KEY = 7291


def _sync_lock(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (SYNC_LOCK_KEY,))


def pick_odometer(row: dict, source: str | None) -> float | None:
    """Pick the odometer value for a normalized row given the vehicle's trusted source."""
    preferred = ODO_ALIASES.get(source)
    order = tuple(ODO_ALIASES.values())
    if preferred is not None:
        order = (preferred,) + tuple(k for k in ODO_ALIASES.values() if k != preferred)
    for key in order:
        value = row.get(key)
        if value is not None:
            return value
    return None


def resolve_odometer_sources(conn) -> dict[str, int]:
    """Scan telemetry and assign each vehicle its trusted odometer source."""
    _sync_lock(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE vehicles v SET odometer_source = '245'
            WHERE EXISTS (SELECT 1 FROM telemetry t
                          WHERE t.vehicle_id = v.intangle_id AND t.raw ? '245')
            """
        )
        defaulted = cur.rowcount
        cur.execute(
            """
            WITH drops AS (
                SELECT vehicle_id FROM (
                    SELECT vehicle_id,
                           (raw->'245'->'value'->>0)::numeric
                             - lag((raw->'245'->'value'->>0)::numeric)
                               OVER (PARTITION BY vehicle_id ORDER BY time) AS d
                    FROM telemetry WHERE raw ? '245'
                ) x WHERE d < %s
            )
            UPDATE vehicles v SET odometer_source = 'EMS_245'
            FROM drops
            WHERE drops.vehicle_id = v.intangle_id
              AND EXISTS (SELECT 1 FROM telemetry t
                          WHERE t.vehicle_id = v.intangle_id AND t.raw ? 'EMS_245')
            """,
            (-DROP_THRESHOLD_KM,),
        )
        resets = cur.rowcount
        cur.execute(
            """
            WITH ratios AS (
                SELECT vehicle_id,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY (raw->'245'->'value'->>0)::numeric) AS med245,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY (raw->'EMS_245'->'value'->>0)::numeric) AS medems
                FROM telemetry
                WHERE raw ? '245' AND raw ? 'EMS_245'
                GROUP BY vehicle_id
            )
            UPDATE vehicles v SET odometer_source = 'EMS_245'
            FROM ratios
            WHERE ratios.vehicle_id = v.intangle_id
              AND ratios.med245 IS NOT NULL AND ratios.medems IS NOT NULL
              AND ratios.med245 > 0 AND ratios.medems > 0
              AND (ratios.med245 / ratios.medems < 0.75 OR ratios.med245 / ratios.medems > 1.25)
            """
        )
        scaled = cur.rowcount
        cur.execute(
            """
            UPDATE vehicles v SET odometer_source = 'EMS_245'
            WHERE NOT EXISTS (SELECT 1 FROM telemetry t
                              WHERE t.vehicle_id = v.intangle_id AND t.raw ? '245')
              AND EXISTS (SELECT 1 FROM telemetry t
                          WHERE t.vehicle_id = v.intangle_id AND t.raw ? 'EMS_245')
            """
        )
        ems_only = cur.rowcount
    return {"default_245": defaulted, "reset_fixed": resets,
            "scaled_fixed": scaled, "ems_only": ems_only}


def recompute_odometer(conn, null_only: bool = False) -> int:
    """Rewrite the odometer column from raw JSONB using each vehicle's source.

    For EMS_245-source vehicles, only EMS_245 is trusted (PID 245 is a
    doubled/bogus register for them); rows lacking EMS_245 get NULL.
    For 245-source vehicles, fall back through EMS_245 → mhn_blazo_245.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE telemetry t
            SET odometer = CASE v.odometer_source
                WHEN 'EMS_245' THEN COALESCE(
                    (t.raw->'EMS_245'->'value'->>0)::numeric)
                ELSE COALESCE(
                    (t.raw->'245'->'value'->>0)::numeric,
                    (t.raw->'EMS_245'->'value'->>0)::numeric,
                    (t.raw->'mhn_blazo_245'->'value'->>0)::numeric)
            END
            FROM vehicles v
            WHERE v.intangle_id = t.vehicle_id
              AND v.odometer_source IS NOT NULL
              AND (t.raw ? '245' OR t.raw ? 'EMS_245' OR t.raw ? 'mhn_blazo_245')
              {'AND t.odometer IS NULL' if null_only else ''}
            """
        )
        return cur.rowcount
