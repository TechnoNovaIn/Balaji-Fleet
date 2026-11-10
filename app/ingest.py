import time
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg
from psycopg.types.json import Json

from app.config import settings
from app.normalize import normalize_gps_record, normalize_record
from app.odometer import SYNC_LOCK_KEY, pick_odometer, recompute_odometer, resolve_odometer_sources


def sync_vehicles(client, conn, resolve_odometer: bool = True) -> int:
    vehicles = client.get_vehicles()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (SYNC_LOCK_KEY,))
        cur.execute("UPDATE vehicles SET is_active = FALSE")
        for v in vehicles:
            cur.execute(
                """
                INSERT INTO vehicles (intangle_id, vehicle_no, first_seen, last_seen, is_active)
                VALUES (%(id)s, %(no)s, now(), now(), TRUE)
                ON CONFLICT (intangle_id, vehicle_no)
                DO UPDATE SET last_seen = now(), is_active = TRUE
                """,
                {"id": v["intangle_id"], "no": v["vehicle_no"]},
            )
        conn.commit()
    # The odometer resolution scans the ENTIRE telemetry table (minutes on
    # millions of rows) while holding the advisory lock, which starves every
    # other process. The live poller keeps odometer sources fresh; a historical
    # backfill doesn't need to repeat it on every run, so it opts out.
    if resolve_odometer:
        resolve_odometer_sources(conn)
        recompute_odometer(conn, null_only=True)
        conn.commit()
    return len(vehicles)


def _day_range(day: date) -> tuple[str, str]:
    from_dt = f"{day.strftime('%Y-%m-%d')} 00:00:00"
    to_dt = f"{day.strftime('%Y-%m-%d')} 23:59:59.999"
    return from_dt, to_dt


def _dt_range(from_dt: datetime, to_dt: datetime) -> tuple[str, str]:
    return from_dt.strftime("%Y-%m-%d %H:%M:%S"), to_dt.strftime("%Y-%m-%d %H:%M:%S")


def _insert_telemetry(conn, rows: list[dict]):
    if not rows:
        return 0
    columns = [
        "vehicle_id", "time", "end_time", "tracker_id",
        "rpm", "speed", "engine_load", "accel_pedal", "fuel_rate",
        "coolant_temp", "intake_temp", "boost_pressure", "oil_pressure",
        "air_inlet_press", "egt", "ambient_temp", "gear",
        "odometer", "engine_hours", "total_fuel", "battery_voltage",
        "axle_load", "axle_load_2", "vehicle_load_cond", "lift_axle", "raw",
    ]
    sql = f"""
        INSERT INTO telemetry ({", ".join(columns)})
        VALUES ({", ".join("%(" + c + ")s" for c in columns)})
        ON CONFLICT (vehicle_id, time) DO NOTHING
    """
    prepared = []
    for r in rows:
        item = dict(r)
        if item.get("raw") is not None and not isinstance(item["raw"], Json):
            item["raw"] = Json(item["raw"])
        prepared.append(item)
    with conn.cursor() as cur:
        cur.executemany(sql, prepared)
        return max(cur.rowcount, 0)


def _insert_gps(conn, rows: list[dict]):
    if not rows:
        return 0
    columns = ["vehicle_id", "vehicle_no", "time", "lat", "lon", "raw"]
    sql = f"""
        INSERT INTO gps_telemetry ({", ".join(columns)})
        VALUES ({", ".join("%(" + c + ")s" for c in columns)})
        ON CONFLICT (vehicle_no, time) DO NOTHING
    """
    prepared = []
    for r in rows:
        item = dict(r)
        if item.get("raw") is not None and not isinstance(item["raw"], Json):
            item["raw"] = Json(item["raw"])
        prepared.append(item)
    with conn.cursor() as cur:
        cur.executemany(sql, prepared)
        return max(cur.rowcount, 0)


def _mark_run(conn, vehicle_no: str, day: date, status: str, records: int = 0, error: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_runs (vehicle_no, day, status, records, error, started_at, finished_at)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (vehicle_no, day)
            DO UPDATE SET status = EXCLUDED.status, records = EXCLUDED.records,
                          error = EXCLUDED.error, finished_at = now()
            """,
            (vehicle_no, day, status, records, error),
        )


def _registry(conn) -> dict[int, set[str]]:
    registry: dict[int, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT intangle_id, vehicle_no FROM vehicles")
        for row in cur.fetchall():
            registry.setdefault(row["intangle_id"], set()).add(row["vehicle_no"])
    return registry


def _odometer_sources(conn) -> dict[int, str | None]:
    sources: dict[int, str | None] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT intangle_id, odometer_source FROM vehicles")
        for row in cur.fetchall():
            sources[row["intangle_id"]] = row["odometer_source"]
    return sources


def ingest_batch(client, conn, plates: list[str], day: date) -> dict[str, int]:
    result: dict[str, int] = {p: 0 for p in plates}
    from_dt, to_dt = _day_range(day)

    try:
        responses = client.get_obd(plates, from_dt, to_dt)
    except httpx.TransportError as exc:
        # Connection reset / timeout / DNS — mark the batch failed and move on so
        # one flaky batch doesn't kill the whole backfill (previously this
        # propagated and supervisord loop-restarted the process repeatedly).
        for p in plates:
            _mark_run(conn, p, day, "error", 0, str(exc))
        conn.commit()
        return result
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            for p in plates:
                _mark_run(conn, p, day, "empty", 0)
            conn.commit()
            return result
        for p in plates:
            _mark_run(conn, p, day, "error", 0, str(exc))
        conn.commit()
        return result

    registry = _registry(conn)
    odometer_sources = _odometer_sources(conn)
    vid_to_plate: dict[int, str] = {}
    for vid, plate_set in registry.items():
        for plate in plates:
            if plate in plate_set:
                vid_to_plate.setdefault(vid, plate)

    seen_vids: set[int] = set()
    total_inserted = 0
    for response in responses:
        status_code = response.get("status", {}).get("code")
        if status_code and status_code != 200:
            continue
        data = response.get("results", {}).get("data", [])
        rows = [normalize_record(record) for record in data]
        rows = [r for r in rows if r is not None]
        if not rows:
            continue
        for row in rows:
            row["odometer"] = pick_odometer(row, odometer_sources.get(row["vehicle_id"]))
        total_inserted += _insert_telemetry(conn, rows)
        for row in rows:
            seen_vids.add(row["vehicle_id"])
        for vid in {row["vehicle_id"] for row in rows}:
            plate = vid_to_plate.get(vid)
            if plate:
                result[plate] += sum(1 for r in rows if r["vehicle_id"] == vid)

    for plate in plates:
        status = "success" if result[plate] else "empty"
        _mark_run(conn, plate, day, status, result[plate])
    conn.commit()
    return result


def ingest_obd_window(client, conn, plates: list[str], from_dt: datetime, to_dt: datetime) -> int:
    from_s, to_s = _dt_range(from_dt, to_dt)
    try:
        responses = client.get_obd(plates, from_s, to_s)
    except httpx.TransportError:
        return 0
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return 0
        raise

    odometer_sources = _odometer_sources(conn)
    rows = []
    for response in responses:
        status_code = response.get("status", {}).get("code")
        if status_code and status_code != 200:
            continue
        for record in response.get("results", {}).get("data", []):
            row = normalize_record(record)
            if row is None:
                continue
            row["odometer"] = pick_odometer(row, odometer_sources.get(row["vehicle_id"]))
            rows.append(row)
    return _insert_telemetry(conn, rows)


def ingest_gps_window(client, conn, plates: list[str], from_dt: datetime, to_dt: datetime) -> int:
    from_s = from_dt.strftime("%Y-%m-%d")
    to_s = to_dt.strftime("%Y-%m-%d")
    try:
        data = client.get_gps(plates, from_s, to_s)
    except httpx.TransportError:
        return 0
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return 0
        raise

    rows = [normalize_gps_record(record) for record in data]
    rows = [row for row in rows if row is not None and row["vehicle_no"]]
    return _insert_gps(conn, rows)


def poll_live_window(
    client,
    conn,
    vehicles: list[str] | None = None,
    lookback_seconds: int = 900,
) -> dict[str, int]:
    # The Intangles API interprets fromDate/toDate as UTC wall-clock, so the
    # window MUST be formatted in UTC (an IST wall-clock window is ~5.5h in the
    # future from the API's point of view and returns nothing). Data storage is
    # epoch-ms so stored times are absolute regardless of this.
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(seconds=lookback_seconds)
    if vehicles is None:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vehicle_no FROM vehicles WHERE is_active")
            plates = [row["vehicle_no"] for row in cur.fetchall()]
    else:
        plates = list(vehicles)

    totals = {"requests": 0, "obd_records": 0, "gps_records": 0}
    batch_size = settings.live_batch_size
    for i in range(0, len(plates), batch_size):
        batch = plates[i : i + batch_size]
        totals["obd_records"] += ingest_obd_window(client, conn, batch, from_dt, now)
        totals["gps_records"] += ingest_gps_window(client, conn, batch, from_dt, now)
        totals["requests"] += 2
        conn.commit()
        time.sleep(settings.ingest_delay_seconds)
    return totals


def backfill(client, conn, days: int, plates: list[str] | None = None, include_today: bool = False) -> dict[str, int]:
    totals = {"requests": 0, "records": 0, "gps_records": 0}
    if plates is None:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vehicle_no FROM vehicles WHERE is_active")
            plates = [row["vehicle_no"] for row in cur.fetchall()]

    today = date.today()
    day_list = [today - timedelta(days=d + 1) for d in range(days)]
    if include_today:
        # Today is normally owned by the live poller, but when the live poller
        # was down for a stretch (e.g. a missed day) this backfills today's full
        # day with the robust batch-5 path instead of the 15-min live window.
        day_list = [today] + day_list

    for day in day_list:
        done_plates: set[str] = set()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vehicle_no FROM ingest_runs WHERE day = %s AND status IN ('success', 'empty')",
                (day,),
            )
            done_plates = {row["vehicle_no"] for row in cur.fetchall()}
        todo = [p for p in plates if p not in done_plates]
        if not todo:
            print(f"[{day}] already ingested, skipping")
            continue
        print(f"[{day}] ingesting {len(todo)} vehicles in batches of {settings.ingest_batch_size}")
        for i in range(0, len(todo), settings.ingest_batch_size):
            batch = todo[i : i + settings.ingest_batch_size]
            result = ingest_batch(client, conn, batch, day)
            totals["requests"] += 1
            batch_records = sum(result.values())
            totals["records"] += batch_records
            print(f"  {batch[0]} .. {batch[-1]}: {batch_records} records")
            from_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            to_dt = from_dt + timedelta(days=1)
            gps = ingest_gps_window(client, conn, batch, from_dt, to_dt)
            totals["gps_records"] += gps
            totals["requests"] += 1
            time.sleep(settings.ingest_delay_seconds)
    return totals


def poll(client, conn, vehicles: list[str] | None = None) -> dict[str, int]:
    totals = {"requests": 0, "records": 0, "gps_records": 0}
    today = date.today()
    day_list = [today - timedelta(days=d + 1) for d in range(settings.poll_lookback_days)]

    for day in day_list:
        if vehicles is not None:
            plates = list(vehicles)
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT v.vehicle_no FROM vehicles v
                    WHERE v.is_active
                      AND NOT EXISTS (
                          SELECT 1 FROM ingest_runs r
                          WHERE r.vehicle_no = v.vehicle_no AND r.day = %s
                            AND r.status IN ('success', 'empty')
                      )
                    """,
                    (day,),
                )
                plates = [row["vehicle_no"] for row in cur.fetchall()]
        if not plates:
            print(f"[{day}] nothing to do")
            continue
        print(f"[{day}] polling {len(plates)} vehicles")
        for i in range(0, len(plates), settings.ingest_batch_size):
            batch = plates[i : i + settings.ingest_batch_size]
            result = ingest_batch(client, conn, batch, day)
            totals["requests"] += 1
            totals["records"] += sum(result.values())
            from_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            to_dt = from_dt + timedelta(days=1)
            gps = ingest_gps_window(client, conn, batch, from_dt, to_dt)
            totals["gps_records"] += gps
            totals["requests"] += 1
            time.sleep(settings.ingest_delay_seconds)
    return totals


def poll_today(client, conn, vehicles: list[str] | None = None) -> dict[str, int]:
    totals = {"requests": 0, "records": 0}
    today = date.today()
    with conn.cursor() as cur:
        if vehicles is None:
            cur.execute("SELECT DISTINCT vehicle_no FROM vehicles WHERE is_active")
            plates = [row["vehicle_no"] for row in cur.fetchall()]
        else:
            plates = list(vehicles)
    for i in range(0, len(plates), settings.ingest_batch_size):
        batch = plates[i : i + settings.ingest_batch_size]
        result = ingest_batch(client, conn, batch, today)
        totals["requests"] += 1
        totals["records"] += sum(result.values())
        time.sleep(settings.ingest_delay_seconds)
    return totals


def needs_backfill(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM vehicles")
        return cur.fetchone()["n"]
