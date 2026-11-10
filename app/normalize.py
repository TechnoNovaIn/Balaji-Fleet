from datetime import datetime, timezone


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pid(pids: dict, key: str):
    block = pids.get(key)
    if not block:
        return None
    values = block.get("value")
    if not values:
        return None
    return values[0]


PID_COLUMNS = {
    "84": "speed",
    "91": "accel_pedal",
    "92": "engine_load",
    "190": "rpm",
    "183": "fuel_rate",
    "110": "coolant_temp",
    "105": "intake_temp",
    "132": "boost_pressure",
    "100": "oil_pressure",
    "106": "air_inlet_press",
    "173": "egt",
    "171": "ambient_temp",
    "899": "gear",
    "245": "odometer",
    "247": "engine_hours",
    "250": "total_fuel",
    "168": "battery_voltage",
    "3250": "axle_load",
    "3251": "axle_load_2",
}

COLUMN_FALLBACKS = {
    "speed": ("mhn_84a", "mhn_84b", "spn_84_avg"),
    "rpm": ("spn_190_avg",),
    "engine_load": ("spn_92_avg",),
    "accel_pedal": ("spn_91_avg",),
    "boost_pressure": ("spn_132_avg",),
    "fuel_rate": ("mhn_185a", "mhn_185b"),
    "axle_load": ("spn_3250_avg",),
    "axle_load_2": ("spn_3251_avg",),
}

STRING_COLUMNS = {
    "mhn_veh_load_cond": "vehicle_load_cond",
    "mhn_lift_axle": "lift_axle",
}

ODOMETER_COLUMNS = {
    "245": "odometer_245",
    "EMS_245": "odometer_ems",
    "mhn_blazo_245": "odometer_mhn",
}


def _int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_record(record: dict) -> dict | None:
    start_ms = record.get("start_time")
    if start_ms is None:
        return None

    pids = {}
    for block in record.get("pids", []) or []:
        pids.update(block)

    row = {
        "vehicle_id": _int(record.get("vehicle_id")),
        "time": datetime.fromtimestamp(int(start_ms) / 1000, tz=timezone.utc),
        "end_time": datetime.fromtimestamp(int(record.get("end_time") or start_ms) / 1000, tz=timezone.utc),
        "tracker_id": _int(record.get("tracker_id")),
        "raw": pids,
    }

    for pid, column in PID_COLUMNS.items():
        row[column] = _num(_pid(pids, pid))

    for column, fallbacks in COLUMN_FALLBACKS.items():
        if row[column] is None:
            for key in fallbacks:
                value = _num(_pid(pids, key))
                if value is not None:
                    row[column] = value
                    break

    for pid, column in ODOMETER_COLUMNS.items():
        row[column] = _num(_pid(pids, pid))

    for pid, column in STRING_COLUMNS.items():
        value = _pid(pids, pid)
        row[column] = str(value) if value is not None else None

    return row


def normalize_gps_record(record: dict) -> dict | None:
    timestamp_ms = record.get("timestamp") or record.get("time")
    if timestamp_ms is None:
        return None

    lat = _num(record.get("lat") or record.get("latitude"))
    lon = _num(record.get("lon") or record.get("longitude") or record.get("lng"))

    return {
        "vehicle_id": _int(record.get("intangle_id") or record.get("vehicle_id")),
        "vehicle_no": str(record.get("vehicle_no") or record.get("vehicleNos") or record.get("vehicle_plate") or "").strip(),
        "time": datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc),
        "lat": lat,
        "lon": lon,
        "raw": record,
    }


def split_by_vehicle(records: list[dict]) -> dict[int, list[dict]]:
    by_vehicle: dict[int, list[dict]] = {}
    for record in records:
        normalized = normalize_record(record)
        if normalized is None:
            continue
        by_vehicle.setdefault(normalized["vehicle_id"], []).append(normalized)
    return by_vehicle
