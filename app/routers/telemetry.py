"""Telemetry endpoints — raw telemetry, live readings, fleet activity, timetable."""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.db import connect
from app.trips import dwells_from_rows

KOLKATA = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger(__name__)

router = APIRouter(tags=["telemetry"])


@router.get("/telemetry")
def read_telemetry(
    vehicle_no: str,
    from_dt: datetime,
    to_dt: datetime,
    limit: int = 1000,
    user: dict = Depends(get_current_user),
):
    sql = """
        SELECT t.* FROM telemetry t
        JOIN vehicles v ON v.intangle_id = t.vehicle_id
        WHERE v.vehicle_no = %s AND t.time >= %s AND t.time <= %s
        ORDER BY t.time LIMIT %s
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (vehicle_no, from_dt, to_dt, limit))
            return cur.fetchall()


@router.get("/telemetry/fleet-activity")
def fleet_activity(day: date = date.today(), user: dict = Depends(get_current_user)):
    from_dt = datetime.combine(day, datetime.min.time())
    to_dt = from_dt + timedelta(days=1)
    sql = """
        SELECT
            to_char(time_bucket('1 hour', t.time AT TIME ZONE 'Asia/Kolkata'), 'HH24:00') AS hour,
            count(DISTINCT t.vehicle_id) FILTER (WHERE t.speed > 1) AS moving,
            count(DISTINCT t.vehicle_id) FILTER (WHERE t.speed <= 1 AND t.rpm > 0) AS idling
        FROM telemetry t
        WHERE t.time >= %s AND t.time < %s
        GROUP BY hour ORDER BY hour
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (from_dt, to_dt))
            return cur.fetchall()


@router.get("/live")
def live_readings(
    minutes: int = 20,
    q: str = "",
    status: str = "all",
    page: int = 1,
    page_size: int = 0,
    user: dict = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    gps_stationary_since = now - timedelta(minutes=20)
    base = """
        SELECT DISTINCT ON (v.vehicle_no)
            v.vehicle_no,
            t.time AS obd_time,
            telem.time AS time,
            telem.rpm, telem.speed, telem.engine_load, telem.accel_pedal,
            telem.fuel_rate, telem.coolant_temp, telem.intake_temp, telem.boost_pressure,
            telem.oil_pressure, telem.air_inlet_press, telem.egt, telem.ambient_temp,
            telem.gear, telem.engine_hours, telem.total_fuel, telem.battery_voltage,
            telem.axle_load, telem.axle_load_2,
            telem.vehicle_load_cond, telem.lift_axle,
            COALESCE(telem.odometer, last_odo.odometer) AS odometer,
            v.payload_tonnes,
            v.total_mass_kg,
            v.tare_mass_kg,
            v.tonnage_note,
            v.tonnage_updated_at,
            v.kerb_weight_kg,
            v.kerb_model,
            v.gvw_kg,
            v.payload_capacity_kg,
            v.trailer_weight_kg,
            v.vehicle_type,
            g.gps_time,
            g.lat,
            g.lon,
            COALESCE(t.time < %s, TRUE) AS stopped,
            COALESCE(
                gps_recent.points >= 2
                AND NOT COALESCE(gps_recent.moved, FALSE)
                AND g.gps_time >= %s,
                FALSE
            ) AS gps_stationary_20m
        FROM vehicles v
        LEFT JOIN LATERAL (
            SELECT * FROM telemetry WHERE vehicle_id = v.intangle_id ORDER BY time DESC LIMIT 1
        ) t ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM telemetry
            WHERE vehicle_id = v.intangle_id
              AND (rpm IS NOT NULL OR speed IS NOT NULL OR engine_load IS NOT NULL
                   OR coolant_temp IS NOT NULL OR fuel_rate IS NOT NULL)
            ORDER BY time DESC LIMIT 1
        ) telem ON TRUE
        LEFT JOIN LATERAL (
            SELECT odometer FROM telemetry
            WHERE vehicle_id = v.intangle_id AND odometer IS NOT NULL
            ORDER BY time DESC LIMIT 1
        ) last_odo ON TRUE
        LEFT JOIN LATERAL (
            SELECT time AS gps_time, lat, lon FROM gps_telemetry
            WHERE vehicle_no = v.vehicle_no
            ORDER BY time DESC LIMIT 1
        ) g ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                count(*) AS points,
                bool_or(abs(gr.lat - g.lat) > 0.0001 OR abs(gr.lon - g.lon) > 0.0001) AS moved
            FROM gps_telemetry gr
            WHERE gr.vehicle_no = v.vehicle_no
              AND gr.time >= %s
              AND gr.lat IS NOT NULL
              AND gr.lon IS NOT NULL
        ) gps_recent ON TRUE
        WHERE v.is_active
    """
    order = " ORDER BY v.vehicle_no, t.time DESC NULLS LAST"
    where = ""
    params = [since, gps_stationary_since, gps_stationary_since]
    if q.strip():
        where += " AND v.vehicle_no ILIKE %s"
        params.append(f"%{q.strip()}%")
    if status in ("live", "stopped"):
        where += " AND COALESCE(t.time < %s, TRUE) = %s"
        params.extend([since, status == "stopped"])
    with connect() as conn:
        with conn.cursor() as cur:
            if page_size > 0:
                cur.execute(f"SELECT count(*) AS n FROM ({base}{where}) sub", params)
                total = cur.fetchone()["n"]
                offset = (page - 1) * page_size
                cur.execute(
                    f"SELECT * FROM ({base}{where}{order}) sub ORDER BY vehicle_no LIMIT %s OFFSET %s",
                    params + [page_size, offset],
                )
                rows = cur.fetchall()
                return {"rows": rows, "total": total, "page": page, "page_size": page_size}
            cur.execute(f"SELECT * FROM ({base}{where}{order}) sub ORDER BY vehicle_no", params)
            return cur.fetchall()


@router.get("/live/day")
def live_readings_by_day(
    day: date,
    q: str = "",
    page: int = 1,
    page_size: int = 0,
    user: dict = Depends(get_current_user),
):
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    base = """
        SELECT DISTINCT ON (v.vehicle_no)
            v.vehicle_no,
            t.time AS obd_time,
            telem.time AS time,
            telem.rpm, telem.speed, telem.engine_load, telem.accel_pedal,
            telem.fuel_rate, telem.coolant_temp, telem.intake_temp, telem.boost_pressure,
            telem.oil_pressure, telem.air_inlet_press, telem.egt, telem.ambient_temp,
            telem.gear, telem.engine_hours, telem.total_fuel, telem.battery_voltage,
            telem.axle_load, telem.axle_load_2,
            telem.vehicle_load_cond, telem.lift_axle,
            COALESCE(telem.odometer, last_odo.odometer) AS odometer,
            v.payload_tonnes,
            v.total_mass_kg,
            v.tare_mass_kg,
            v.tonnage_note,
            v.kerb_weight_kg,
            v.kerb_model,
            v.gvw_kg,
            v.payload_capacity_kg,
            v.trailer_weight_kg,
            v.vehicle_type,
            (t.time IS NULL) AS stopped,
            (t.time IS NULL) AS gps_stationary_20m,
            NULL AS gps_time,
            NULL AS lat,
            NULL AS lon
        FROM vehicles v
        LEFT JOIN LATERAL (
            SELECT * FROM telemetry
            WHERE vehicle_id = v.intangle_id AND time >= %s AND time < %s
            ORDER BY time DESC LIMIT 1
        ) t ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM telemetry
            WHERE vehicle_id = v.intangle_id
              AND time >= %s AND time < %s
              AND (rpm IS NOT NULL OR speed IS NOT NULL OR engine_load IS NOT NULL
                   OR coolant_temp IS NOT NULL OR fuel_rate IS NOT NULL)
            ORDER BY time DESC LIMIT 1
        ) telem ON TRUE
        LEFT JOIN LATERAL (
            SELECT odometer FROM telemetry
            WHERE vehicle_id = v.intangle_id AND odometer IS NOT NULL
              AND time >= %s AND time < %s
            ORDER BY time DESC LIMIT 1
        ) last_odo ON TRUE
        WHERE v.is_active
    """
    order = " ORDER BY v.vehicle_no, t.time DESC NULLS LAST"
    where = ""
    params = [day_start, day_end, day_start, day_end, day_start, day_end]
    if q.strip():
        where += " AND v.vehicle_no ILIKE %s"
        params.append(f"%{q.strip()}%")
    with connect() as conn:
        with conn.cursor() as cur:
            if page_size > 0:
                cur.execute(f"SELECT count(*) AS n FROM ({base}{where}) sub", params)
                total = cur.fetchone()["n"]
                offset = (page - 1) * page_size
                cur.execute(
                    f"SELECT * FROM ({base}{where}{order}) sub ORDER BY vehicle_no LIMIT %s OFFSET %s",
                    params + [page_size, offset],
                )
                rows = cur.fetchall()
                return {"rows": rows, "total": total, "page": page, "page_size": page_size}
            cur.execute(f"SELECT * FROM ({base}{where}{order}) sub ORDER BY vehicle_no", params)
            return cur.fetchall()


@router.get("/timetable")
def timetable(day: date, q: str = "", user: dict = Depends(get_current_user)):
    """24-hour load schedule for a given day (IST), computed from stored telemetry."""
    from_dt = datetime.combine(day, datetime.min.time())
    to_dt = from_dt + timedelta(days=1)
    plate_sql = "SELECT DISTINCT vehicle_no, payload_tonnes FROM vehicles WHERE is_active"
    plate_params: list = []
    if q.strip():
        plate_sql += " AND vehicle_no ILIKE %s"
        plate_params.append(f"%{q.strip()}%")
    plate_sql += " ORDER BY vehicle_no"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(plate_sql, plate_params)
            plates = cur.fetchall()
    if not plates:
        return []
    plate_nos = [p["vehicle_no"] for p in plates]
    where = "v.is_active AND v.vehicle_no = ANY(%s) AND t.time >= %s AND t.time < %s"
    params: list = [plate_nos, from_dt, to_dt]
    sql = f"""
        SELECT
            v.vehicle_no,
            v.payload_tonnes,
            extract(hour FROM time_bucket('1 hour', t.time AT TIME ZONE 'Asia/Kolkata'))::int AS hour,
            count(*) AS total,
            count(*) FILTER (WHERE t.speed > 1 OR t.rpm > 0) AS running,
            count(*) FILTER (WHERE t.vehicle_load_cond = '10') AS c_loaded,
            count(*) FILTER (WHERE t.vehicle_load_cond = '11') AS c_partial,
            count(*) FILTER (WHERE t.vehicle_load_cond = '01') AS c_empty
        FROM telemetry t
        JOIN vehicles v ON v.intangle_id = t.vehicle_id
        WHERE {where}
        GROUP BY v.vehicle_no, v.payload_tonnes, extract(hour FROM time_bucket('1 hour', t.time AT TIME ZONE 'Asia/Kolkata'))
        ORDER BY v.vehicle_no
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    by_vehicle: dict = {}
    for r in rows:
        entry = by_vehicle.setdefault(
            r["vehicle_no"],
            {"vehicle_no": r["vehicle_no"], "payload_tonnes": r["payload_tonnes"], "hours": {}},
        )
        entry["hours"][r["hour"]] = r

    trip_map: dict = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_no,
                       extract(hour FROM start_time AT TIME ZONE 'Asia/Kolkata')::int AS start_hour,
                       extract(hour FROM end_time AT TIME ZONE 'Asia/Kolkata')::int AS end_hour,
                       load_label, trip_type, start_event, end_event,
                       distance_km, avg_payload_t, fuel_per_km,
                       start_time, end_time
                FROM trips
                WHERE day = %s AND vehicle_no = ANY(%s)
                ORDER BY vehicle_no, start_time
                """,
                (day, plate_nos),
            )
            for r in cur.fetchall():
                trip_map.setdefault(r["vehicle_no"], []).append({
                    "start_hour": r["start_hour"],
                    "end_hour": r["end_hour"],
                    "load_label": r["load_label"],
                    "trip_type": r["trip_type"],
                    "start_event": r["start_event"],
                    "end_event": r["end_event"],
                    "distance_km": r["distance_km"],
                    "avg_payload_t": r["avg_payload_t"],
                    "fuel_per_km": r["fuel_per_km"],
                    "start_time": r["start_time"],
                    "end_time": r["end_time"],
                })
    out = []
    for plate in plates:
        vehicle_no = plate["vehicle_no"]
        entry = by_vehicle.get(vehicle_no)
        has_data = entry is not None
        entry = entry or {"vehicle_no": vehicle_no, "payload_tonnes": plate["payload_tonnes"], "hours": {}}
        schedule = []
        idle_frac = {}
        trips = trip_map.get(vehicle_no, [])
        for hour in range(24):
            h = entry["hours"].get(hour)
            if not h or h["total"] == 0:
                hour_state = "no load"
            else:
                flagged = h["c_loaded"] + h["c_partial"] + h["c_empty"]
                if flagged > 0:
                    if h["c_loaded"] >= h["c_partial"] and h["c_loaded"] >= h["c_empty"]:
                        hour_state = "loaded"
                    elif h["c_partial"] >= h["c_empty"]:
                        hour_state = "partial load"
                    else:
                        hour_state = "no load"
                elif h["running"] > 0:
                    hour_state = "loaded" if (entry["payload_tonnes"] or 0) > 0 else "partial load"
                else:
                    hour_state = "no load"
            for t in trips:
                if t["start_hour"] <= hour <= t["end_hour"] and t["load_label"] == "loaded":
                    if h and h["total"] > 0 and h["running"] is not None:
                        idle_frac[hour] = 1.0 - (h["running"] / h["total"])
                    elif not h or h["total"] == 0:
                        idle_frac[hour] = 1.0
                    else:
                        idle_frac[hour] = 0.0
                    if hour_state == "no load":
                        hour_state = "loaded"
                    elif hour_state == "partial load":
                        hour_state = "loaded"
                    break
            schedule.append(hour_state)
        out.append({
            "vehicle_no": vehicle_no,
            "payload_tonnes": entry["payload_tonnes"],
            "has_data": has_data,
            "schedule": schedule,
            "idle_frac": idle_frac,
            "trips": trip_map.get(vehicle_no, []),
            "dwells": [
                {
                    "type": d["type"],
                    "start": d["start"],
                    "end": d["end"],
                    "start_hour": d["start"].astimezone(KOLKATA).hour,
                    "end_hour": d["end"].astimezone(KOLKATA).hour,
                    "duration_min": d["duration_min"],
                    "why": d["why"],
                    "delta_t": d["delta_t"],
                }
                for d in dwells_from_rows(trip_map.get(vehicle_no, []))
            ],
        })
    return out


@router.get("/telemetry/day-summary")
def day_summary(vehicle_no: str, day: date, user: dict = Depends(get_current_user)):
    from_dt = datetime.combine(day, datetime.min.time())
    to_dt = from_dt + timedelta(days=1)
    sql = """
        SELECT
            to_char(time_bucket('1 hour', t.time AT TIME ZONE 'Asia/Kolkata'), 'HH24:00') AS hour,
            round(avg(speed)::numeric, 1) AS avg_speed,
            max(speed) AS max_speed,
            round(avg(rpm)::numeric, 0) AS avg_rpm,
            round(avg(engine_load)::numeric, 0) AS avg_load,
            round(avg(fuel_rate)::numeric, 2) AS avg_fuel,
            count(*) FILTER (WHERE speed > 1) AS moving_records,
            count(*) FILTER (WHERE speed <= 1 AND rpm > 0) AS idling_records
        FROM telemetry t
        JOIN vehicles v ON v.intangle_id = t.vehicle_id
        WHERE v.vehicle_no = %s AND t.time >= %s AND t.time < %s
        GROUP BY hour ORDER BY hour
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (vehicle_no, from_dt, to_dt))
            return cur.fetchall()
