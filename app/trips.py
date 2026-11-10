"""Trip segmentation + per-trip empty/loaded comparison.

Splits each vehicle's telemetry stream into TRIPS -- the unit the fleet
actually thinks in: "truck leaves A empty (A->B), loads at B, returns loaded
(B->C)". A trip is a contiguous driving run:

  - bounded by gaps > 10 min between moving samples (a long parked/engine-off
    stretch starts a new trip), which naturally puts the wait at a loading
    yard between the empty leg and the loaded leg;
  - each trip gets stats (distance, duration, idle, fuel, km/L, avg engine
    load, avg rpm) + the GPS start/end point from gps_telemetry;
  - each trip is labelled LOADED / EMPTY / PARTIAL / UNKNOWN from the
    supervised payload regressor (trained on the shipment manifest) with the
    loaded/empty classifier as fallback / confidence.

Writes to the `trips` table (one row per vehicle-day-trip, idempotent:
DELETE+INSERT per vehicle-day). `GET /vehicles/{no}/trip-comparison`
aggregates trips per label so the admin can compare how a truck's OBD behaves
EMPTY (A->B) vs LOADED (B->C) on the same vehicle.

Fully local. No cloud AI.
"""

import bisect
import statistics
from datetime import datetime, timedelta, timezone

import numpy as np

from app.db import connect
from app.tonnage import _clean, JUNK_RPM0_SPEED_MIN
from app.ml_tonnage import (classify_windows_prob, get_models, sliding_windows,
                            load_calibration)
from app.tonnage_regression import predict_windows_payload, get_regressor
IST = timezone(timedelta(hours=5, minutes=30))

MOVING_MIN = 2.0           # km/h to count as a moving sample
GAP_MAX_SECONDS = 10 * 60  # gap > 10 min between moving samples = new trip
MIN_TRIP_SECONDS = 90      # drop shunting blips shorter than this
MIN_TRIP_DISTANCE_M = 1000 # drop shunting blips shorter than this
TRIP_LOADED_T = 10.0       # mean payload (t) above this labels a trip LOADED
GPS_MAX_DELTA_S = 300      # nearest GPS sample window for trip start/end


def _telemetry_for(plate, since):
    """Telemetry rows for one vehicle since `since` (list of dicts, tz-aware)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.time, t.speed, t.rpm, t.gear, t.fuel_rate, t.engine_load,
                       t.accel_pedal, t.vehicle_load_cond, v.intangle_id
                FROM telemetry t
                JOIN vehicles v ON v.intangle_id = t.vehicle_id
                WHERE v.vehicle_no = %s AND t.time >= %s
                ORDER BY t.time
                """,
                (plate, since),
            )
            return [dict(r) for r in cur.fetchall()]


def _gps_for(plate, since):
    """GPS rows for one vehicle since `since` (list of dicts, tz-aware)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT time, lat, lon FROM gps_telemetry
                WHERE vehicle_no = %s AND time >= %s ORDER BY time
                """,
                (plate, since),
            )
            return [dict(r) for r in cur.fetchall()]


def segment_trips(rows):
    """Split telemetry rows (ordered by time) into trips.

    Junk hold-frames (speed > 5 with rpm = 0) are dropped first, as in the
    tonnage solver. A new trip starts whenever the gap between consecutive
    MOVING samples exceeds GAP_MAX_SECONDS -- this keeps engine-on waits at
    traffic lights inside a trip but separates legs across a long stop.
    """
    rows = [
        r for r in rows
        if not (r.get("speed") and r.get("speed") > JUNK_RPM0_SPEED_MIN and r.get("rpm") == 0)
    ]
    trips, cur = [], []
    last_moving = None
    for r in rows:
        if (r.get("speed") or 0) < MOVING_MIN:
            if cur:
                cur.append(r)
            continue
        if cur and last_moving is not None and (r["time"] - last_moving).total_seconds() > GAP_MAX_SECONDS:
            trips.append(cur)
            cur = []
        cur.append(r)
        last_moving = r["time"]
    if cur:
        trips.append(cur)
    return trips


def _trip_stats(trip):
    """Per-trip summary dict, or None if too short to count as a leg."""
    t0, t1 = trip[0]["time"], trip[-1]["time"]
    dur_s = (t1 - t0).total_seconds()
    dist, mov_s, fuel = 0.0, 0.0, 0.0
    for i in range(1, len(trip)):
        prev, cur = trip[i - 1], trip[i]
        dt = (cur["time"] - prev["time"]).total_seconds()
        if dt <= 0 or dt > GAP_MAX_SECONDS:
            continue
        v_prev, v_cur = prev.get("speed") or 0, cur.get("speed") or 0
        dist += (v_prev + v_cur) / 2.0 / 3.6 * dt
        if v_prev >= MOVING_MIN and v_cur >= MOVING_MIN:
            mov_s += dt
        fuel += (prev.get("fuel_rate") or 0) * dt / 3600.0
    if dur_s < MIN_TRIP_SECONDS or dist < MIN_TRIP_DISTANCE_M:
        return None
    moving = [r for r in trip if (r.get("speed") or 0) >= MOVING_MIN]
    if not moving:
        return None
    loads = [r["engine_load"] for r in moving if r.get("engine_load") is not None]
    rpms = [r["rpm"] for r in moving if r.get("rpm")]
    max_speed = max((r.get("speed") or 0) for r in moving)
    avg_speed = (dist / 1000.0) / (mov_s / 3600.0) if mov_s > 0 else 0.0
    dist_km = dist / 1000.0
    return {
        "start_time": t0,
        "end_time": t1,
        "duration_min": round(dur_s / 60, 1),
        "moving_min": round(mov_s / 60, 1),
        "idle_min": round(max(0.0, dur_s - mov_s) / 60, 1),
        "distance_km": round(dist_km, 2),
        "avg_speed_kmh": round(avg_speed, 1),
        "max_speed_kmh": round(max_speed, 1),
        "fuel_used_l": round(fuel, 2),
        "fuel_per_km": round(fuel / dist_km, 3) if dist_km > 0 else None,
        "avg_engine_load": round(statistics.mean(loads), 1) if loads else None,
        "avg_rpm": round(statistics.mean(rpms), 0) if rpms else None,
    }


def _label_trip(trip, regressor, classifier, plate=None):
    """(label, avg_payload_t, loaded_frac) for one trip."""
    clean = _clean(trip)
    if len(clean) < 6:
        # Short yard moves / mostly-idle shunting collapse under the dynamics
        # cleaner (it drops everything <2 km/h), leaving "unknown" with no
        # payload -- which then hides the real loading/unloading windows. Keep
        # stationary rows for short trips so they still get a payload estimate.
        clean = _light_clean(trip)
    if len(clean) < 4:
        return "unknown", None, None
    wins = sliding_windows(clean)
    loaded_frac = None
    if classifier is not None:
        probs = classify_windows_prob(wins, classifier)
        if probs is not None:
            loaded_frac = round(float(np.mean(probs)), 3)
    calibration = load_calibration()
    if regressor is not None:
        pred = predict_windows_payload(wins, regressor, vehicle_no=plate, calibration=calibration)
        if pred is not None:
            avg_payload = round(float(np.mean([max(0.0, p) for p in pred])), 2)
            label = "loaded" if avg_payload >= TRIP_LOADED_T else "empty"
            return label, avg_payload, loaded_frac
    if loaded_frac is None:
        return "unknown", None, None
    if loaded_frac >= 0.6:
        return "loaded", None, loaded_frac
    if loaded_frac <= 0.4:
        return "empty", None, loaded_frac
    return "partial", None, loaded_frac


def _light_clean(rows):
    """Drop junk hold frames (rpm=0 but rolling) but KEEP idle/stationary rows."""
    out = []
    for r in rows:
        speed, rpm = r.get("speed"), r.get("rpm")
        if speed is None or rpm is None:
            continue
        if speed > 5 and rpm == 0:
            continue
        out.append(r)
    return out


def _nearest_gps(times, rows, t, max_delta_s=GPS_MAX_DELTA_S):
    """(lat, lon) of the GPS sample nearest `t`, within max_delta_s."""
    if not times:
        return None, None
    idx = bisect.bisect_left(times, t)
    best = None
    for j in (idx - 1, idx):
        if 0 <= j < len(times):
            d = abs((times[j] - t).total_seconds())
            if d <= max_delta_s and (best is None or d < best[0]):
                best = (d, rows[j])
    if best is None:
        return None, None
    return best[1]["lat"], best[1]["lon"]


def compute_vehicle_trips(plate, days=7, verbose=False):
    """Segment + label + store trips for one vehicle over the last N days.

    Idempotent: each vehicle-day is DELETE'd then re-INSERTed, so re-runs with
    more data just overwrite. Returns the number of trips stored.
    """
    regressor = get_regressor()
    classifier, _ = get_models()
    since = datetime.now(IST) - timedelta(days=days)
    rows = _telemetry_for(plate, since)
    if not rows:
        return 0
    intangle_id = rows[0]["intangle_id"]
    gps = _gps_for(plate, since)
    gps_times = [g["time"] for g in gps]

    by_day = {}
    for r in rows:
        by_day.setdefault(r["time"].astimezone(IST).date(), []).append(r)

    written = 0
    for day, day_rows in sorted(by_day.items()):
        out = []
        # detect dead-band gaps: stretches > GAP_MAX_SECONDS with NO samples
        gaps = []
        for i in range(1, len(day_rows)):
            dt = (day_rows[i]["time"] - day_rows[i - 1]["time"]).total_seconds()
            if dt > GAP_MAX_SECONDS:
                gaps.append((day_rows[i - 1]["time"], day_rows[i]["time"], dt))
        trip_list = segment_trips(day_rows)
        # pass 1: compute stats + label for every valid trip
        enriched = []
        for trip in trip_list:
            stats = _trip_stats(trip)
            if stats is None:
                continue
            label, avg_payload, loaded_frac = _label_trip(trip, regressor, classifier, plate)
            start_lat, start_lng = _nearest_gps(gps_times, gps, trip[0]["time"])
            end_lat, end_lng = _nearest_gps(gps_times, gps, trip[-1]["time"])
            enriched.append({
                "trip": trip, "stats": stats, "label": label,
                "avg_payload": avg_payload, "loaded_frac": loaded_frac,
                "start_lat": start_lat, "start_lng": start_lng,
                "end_lat": end_lat, "end_lng": end_lat,
            })
        # pass 2: attach gap_boundary + prev/next payload across dead-band gaps,
        # and give every trip a SEMANTIC start/end reason so the timetable never
        # shows an "end" for a trip that had no real "start".
        #   start_event / end_event in {day, stream, gap, to_load, to_unload,
        #                               dep_loaded, dep_empty}
        #   - stream:    boundary is just a traffic stop within continuous running
        #   - gap:       dead-band gap (>10 min, no telemetry) -- parked/rest/yard
        #   - to_load:   payload rises >= half TRIP_LOADED_T across the gap ->
        #                trip ARRIVED at a loading point (ends empty)
        #   - dep_loaded: the trip DEPARTED after that loading (starts loaded)
        #   - to_unload: payload drops >= half TRIP_LOADED_T across the gap ->
        #                trip ARRIVED to unload (ends loaded)
        #   - dep_empty: the trip DEPARTED after unloading (starts empty)
        n = len(enriched)
        for e in enriched:
            e["start_event"] = "stream"
            e["end_event"] = "stream"
        for ti in range(n - 1):
            gap_s = (enriched[ti + 1]["trip"][0]["time"] - enriched[ti]["trip"][-1]["time"]).total_seconds()
            if gap_s <= GAP_MAX_SECONDS:
                continue
            p_prev = enriched[ti]["avg_payload"]
            p_next = enriched[ti + 1]["avg_payload"]
            if p_prev is not None and p_next is not None:
                step = p_next - p_prev
                if step >= TRIP_LOADED_T / 2:
                    ev_prev, ev_next = "to_load", "dep_loaded"
                elif step <= -(TRIP_LOADED_T / 2):
                    ev_prev, ev_next = "to_unload", "dep_empty"
                else:
                    ev_prev = ev_next = "gap"
            else:
                ev_prev = ev_next = "gap"
            enriched[ti]["end_event"] = ev_prev
            enriched[ti + 1]["start_event"] = ev_next
            enriched[ti]["gap_boundary"] = True
            enriched[ti + 1]["gap_boundary"] = True
            enriched[ti]["next_payload_t"] = p_next
            enriched[ti + 1]["prev_payload_t"] = p_prev
        if enriched:
            enriched[0]["start_event"] = "day"
            enriched[-1]["end_event"] = "day"
        for e in enriched:
            label = e["label"]
            if label == "loaded":
                trip_type = "loaded_leg"
            elif label == "empty":
                trip_type = "empty_leg"
            elif label == "partial":
                trip_type = "shunting"
            else:
                trip_type = "unknown"
            out.append({
                **e["stats"],
                "start_lat": e["start_lat"], "start_lng": e["start_lng"],
                "end_lat": e["end_lat"], "end_lng": e["end_lng"],
                "load_label": e["label"], "avg_payload_t": e["avg_payload"],
                "loaded_frac": e["loaded_frac"],
                "gap_boundary": e.get("gap_boundary", False),
                "prev_payload_t": e.get("prev_payload_t"),
                "next_payload_t": e.get("next_payload_t"),
                "trip_type": trip_type,
                "start_event": e["start_event"],
                "end_event": e["end_event"],
            })
        write_day(intangle_id, plate, day, out)
        written += len(out)
        if verbose and out:
            print(f"  {plate} {day}: {len(out)} trips")
    return written


_TRIP_COLS = [
    "vehicle_id", "vehicle_no", "day", "trip_no",
    "start_time", "end_time", "distance_km", "duration_min", "moving_min",
    "idle_min", "avg_speed_kmh", "max_speed_kmh", "fuel_used_l",
    "fuel_per_km", "avg_engine_load", "avg_rpm",
    "start_lat", "start_lng", "end_lat", "end_lng",
    "load_label", "avg_payload_t", "loaded_frac",
    "gap_boundary", "prev_payload_t", "next_payload_t",
    "trip_type", "start_event", "end_event",
]


def write_day(vehicle_id, vehicle_no, day, trips):
    """Replace all trips for (vehicle_id, day) atomically."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trips WHERE vehicle_id=%s AND day=%s", (vehicle_id, day))
            for i, t in enumerate(trips, 1):
                cur.execute(
                    f"INSERT INTO trips ({','.join(_TRIP_COLS)}) "
                    f"VALUES ({','.join(['%s'] * len(_TRIP_COLS))})",
                    (vehicle_id, vehicle_no, day, i,
                     t["start_time"], t["end_time"], t["distance_km"], t["duration_min"],
                     t["moving_min"], t["idle_min"], t["avg_speed_kmh"], t["max_speed_kmh"],
                     t["fuel_used_l"], t["fuel_per_km"], t["avg_engine_load"], t["avg_rpm"],
                     t["start_lat"], t["start_lng"], t["end_lat"], t["end_lng"],
                     t["load_label"], t.get("avg_payload_t"), t.get("loaded_frac"),
                     t.get("gap_boundary", False), t.get("prev_payload_t"), t.get("next_payload_t"),
                     t.get("trip_type"), t.get("start_event"), t.get("end_event")),
                )
        conn.commit()


def trips_for_day(plate, day):
    """Stored trips for one vehicle+day; computes on the fly if not present."""
    def _read():
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM trips WHERE vehicle_no=%s AND day=%s ORDER BY trip_no",
                    (plate, day),
                )
                return cur.fetchall()
    rows = _read()
    if not rows:
        compute_vehicle_trips(plate, days=7)
        rows = _read()
    return rows


def compute_all(days, vehicles=None, verbose=True):
    """Compute trips for every active vehicle with recent telemetry (or a
    specific vehicle list). Returns total trips stored."""
    since = datetime.now(IST) - timedelta(days=days)
    with connect() as conn:
        with conn.cursor() as cur:
            if vehicles:
                cur.execute(
                    """
                    SELECT v.intangle_id, v.vehicle_no FROM vehicles v
                    WHERE v.vehicle_no = ANY(%s) AND v.is_active
                    GROUP BY v.intangle_id, v.vehicle_no ORDER BY v.vehicle_no
                    """,
                    (list(vehicles),),
                )
            else:
                cur.execute(
                    """
                    SELECT v.intangle_id, v.vehicle_no
                    FROM vehicles v JOIN telemetry t ON t.vehicle_id = v.intangle_id
                    WHERE t.time >= %s AND v.is_active
                    GROUP BY v.intangle_id, v.vehicle_no ORDER BY v.vehicle_no
                    """,
                    (since,),
                )
            vehs = cur.fetchall()
    total = 0
    for v in vehs:
        try:
            total += compute_vehicle_trips(v["vehicle_no"], days, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  {v['vehicle_no']}: ERROR {e}", flush=True)
    return total


def trip_comparison(plate, days=14):
    """Aggregate stored trips by load label: EMPTY (A->B) vs LOADED (B->C).

    Also infers lading vs. unloading windows anchored on dead-band gaps:
    when a trip is gap_boundary and its prev_payload_t vs its own payload shows
    a +step, the gap is treated as a lading window; a -step is an unloading
    window. This gives an approximate count of load/unload transitions per
    vehicle over the window even though the gap itself had no telemetry.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT load_label, distance_km, duration_min, moving_min,
                       fuel_used_l, avg_engine_load, avg_rpm, avg_speed_kmh,
                       avg_payload_t, gap_boundary, prev_payload_t,
                       start_time, end_time
                FROM trips
                WHERE vehicle_no = %s AND start_time >= %s
                ORDER BY start_time
                """,
                (plate, datetime.now(IST) - timedelta(days=days)),
            )
            rows = cur.fetchall()

    buckets = {
        lab: {"n": 0, "dist": 0.0, "dur": 0.0, "mov": 0.0, "fuel": 0.0,
              "load": 0.0, "rpm": 0.0, "speed": 0.0}
        for lab in ("loaded", "empty")
    }
    n_other = 0
    n_lading, n_unloading = 0, 0
    for r in rows:
        lab = r["load_label"]
        if lab not in buckets:
            if lab in ("partial", "unknown"):
                n_other += 1
            continue
        # infer load/unload transitions across dead-band gaps
        if r["gap_boundary"] and r["prev_payload_t"] is not None and r["avg_payload_t"] is not None:
            delta_t = r["avg_payload_t"] - r["prev_payload_t"]
            if delta_t >= TRIP_LOADED_T / 2:   # loaded ~ +10t or more => lading
                n_lading += 1
            elif delta_t <= -(TRIP_LOADED_T / 2):  # dropped load => unloading
                n_unloading += 1
        b = buckets[lab]
        b["n"] += 1
        b["dist"] += r["distance_km"] or 0
        b["dur"] += r["duration_min"] or 0
        b["mov"] += r["moving_min"] or 0
        b["fuel"] += r["fuel_used_l"] or 0
        b["load"] += r["avg_engine_load"] or 0
        b["rpm"] += r["avg_rpm"] or 0
        b["speed"] += r["avg_speed_kmh"] or 0

    def summarize(b):
        n = b["n"]
        if not n:
            return None
        return {
            "n_trips": n,
            "avg_distance_km": round(b["dist"] / n, 2),
            "avg_duration_min": round(b["dur"] / n, 1),
            "avg_speed_kmh": round(b["speed"] / n, 1),
            "total_distance_km": round(b["dist"], 1),
            "total_fuel_l": round(b["fuel"], 1),
            "fuel_per_km": round(b["fuel"] / b["dist"], 3) if b["dist"] > 0 else None,
            "km_per_l": round(b["dist"] / b["fuel"], 2) if b["fuel"] > 0 else None,
            "avg_engine_load": round(b["load"] / n, 1),
            "avg_rpm": round(b["rpm"] / n, 0),
        }

    loaded = summarize(buckets["loaded"])
    empty = summarize(buckets["empty"])
    delta = None
    if loaded and empty:
        def pct(a, b):
            return round((a - b) / b * 100, 1) if b else None
        delta = {
            "fuel_per_km_pct": pct(loaded["fuel_per_km"], empty["fuel_per_km"]),
            "km_per_l_pct": pct(loaded["km_per_l"], empty["km_per_l"]),
            "avg_load_pct": pct(loaded["avg_engine_load"], empty["avg_engine_load"]),
            "avg_rpm_pct": pct(loaded["avg_rpm"], empty["avg_rpm"]),
            "avg_speed_pct": pct(loaded["avg_speed_kmh"], empty["avg_speed_kmh"]),
        }
    return {
        "vehicle_no": plate, "days": days,
        "loaded": loaded, "empty": empty, "delta": delta,
        "n_trips_other": n_other,
        "has_data": bool(rows),
        "gap_inferred_lading": n_lading,
        "gap_inferred_unloading": n_unloading,
    }


def dwells_from_rows(rows, min_gap_minutes=10):
    """LOATING/UNLOADING/STOP bands from a sorted list of trip dicts.

    `rows` must have: trip_no, start_time, end_time, load_label, avg_payload_t.
    Loading and unloading are not instantaneous markers -- a truck typically
    sits at the loader/receiver for ~1 hour. Each dead-band gap between two
    trips IS that window: the truck arrives (prev trip ends), the load changes
    there, then it leaves (next trip starts). We label the whole gap and give
    the reason ("why") plus the payload step ("how much").

      - LOADING:   payload rose by >= TRIP_LOADED_T across the gap
                   (empty truck in -> loaded truck out)
      - UNLOADING: payload dropped by >= TRIP_LOADED_T across the gap
                   (loaded truck in -> empty truck out)
      - STOP:      parked stop with no material load change (rest/fuel/wait)

    Trips separated by only a small gap (a traffic stop) are not dwells.
    """
    events = []
    for i in range(1, len(rows)):
        prev, nxt = rows[i - 1], rows[i]
        gap_min = (nxt["start_time"] - prev["end_time"]).total_seconds() / 60.0
        if gap_min < min_gap_minutes:
            continue
        p_before = prev["avg_payload_t"]
        p_after = nxt["avg_payload_t"]
        delta = (p_before is not None and p_after is not None and
                 round(p_after - p_before, 1)) or None
        # Classify by the payload step where we have one, otherwise by the
        # trip labels themselves (empty-in/loaded-out = loading, etc.) so a
        # short un-quantified yard trip doesn't turn a real loading window
        # into a generic "parked stop".
        if delta is not None and delta >= TRIP_LOADED_T:
            etype = "loading"
        elif delta is not None and delta <= -TRIP_LOADED_T:
            etype = "unloading"
        elif delta is None:
            lb, la = prev.get("load_label"), nxt.get("load_label")
            if la == "loaded" and lb in ("empty", "unknown"):
                etype = "loading"
            elif lb == "loaded" and la in ("empty", "unknown"):
                etype = "unloading"
            else:
                etype = "stop"
        elif delta is not None:
            etype = "stop"
        if etype == "loading":
            why = ("Empty truck arrived at the loader and left loaded: "
                   + (f"+{delta:.1f}t on board ({p_before:.1f}t -> {p_after:.1f}t)"
                      if delta is not None else "load taken on at this stop"))
        elif etype == "unloading":
            why = ("Loaded truck arrived at the receiver and offloaded: "
                   + (f"-{abs(delta):.1f}t ({p_before:.1f}t -> {p_after:.1f}t)"
                      if delta is not None else "load dropped off at this stop"))
        else:
            why = (f"Parked stop ({gap_min:.0f} min)"
                   + (f", load unchanged ~{p_before:.1f}t" if p_before is not None else "")
                   + "; rest / fuel / waiting")
        events.append({
            "type": etype,
            "start": prev["end_time"],
            "end": nxt["start_time"],
            "duration_min": round(gap_min, 1),
            "why": why,
            "payload_before_t": p_before,
            "payload_after_t": p_after,
            "delta_t": delta,
            "trip_no_before": prev.get("trip_no"),
            "trip_no_after": nxt.get("trip_no"),
        })
    return events


def dwell_events(plate, day, min_gap_minutes=10):
    """LOADING / UNLOADING / STOP windows for a vehicle-day, with real timing.

    See dwells_from_rows for the semantics. Returns a dict with `events`
    (each with start/end/duration_min/type/why/payload_before/after/delta_t)
    plus counts and the average loading/unloading duration.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trip_no, start_time, end_time, load_label, avg_payload_t
                FROM trips
                WHERE vehicle_no = %s AND day = %s
                ORDER BY start_time
                """,
                (plate, day),
            )
            rows = cur.fetchall()

    events = dwells_from_rows(rows)

    return {
        "vehicle_no": plate, "day": day,
        "events": events,
        "n_loading": sum(1 for e in events if e["type"] == "loading"),
        "n_unloading": sum(1 for e in events if e["type"] == "unloading"),
        "n_stops": sum(1 for e in events if e["type"] == "stop"),
        "avg_loading_min": round(
            statistics.mean([e["duration_min"] for e in events if e["type"] == "loading"]), 1)
        if any(e["type"] == "loading" for e in events) else None,
        "avg_unloading_min": round(
            statistics.mean([e["duration_min"] for e in events if e["type"] == "unloading"]), 1)
        if any(e["type"] == "unloading" for e in events) else None,
    }


def dwell_summary(plate, days=14):
    """Fleet-facing dwell summary over N days: how long loading/unloading
    actually takes on this vehicle, and how often it loads/unloads."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT day FROM trips
                WHERE vehicle_no = %s AND start_time >= %s
                ORDER BY day
                """,
                (plate, datetime.now(IST) - timedelta(days=days)),
            )
            days_list = [r["day"] for r in cur.fetchall()]
    load_min, unload_min, n_load, n_unload = [], [], 0, 0
    for d in days_list:
        res = dwell_events(plate, d)
        load_min += [e["duration_min"] for e in res["events"] if e["type"] == "loading"]
        unload_min += [e["duration_min"] for e in res["events"] if e["type"] == "unloading"]
        n_load += res["n_loading"]
        n_unload += res["n_unloading"]
    return {
        "vehicle_no": plate, "days": days,
        "loading_count": n_load,
        "avg_loading_min": round(statistics.mean(load_min), 1) if load_min else None,
        "unloading_count": n_unload,
        "avg_unloading_min": round(statistics.mean(unload_min), 1) if unload_min else None,
        "total_dwells": n_load + n_unload,
        "has_data": bool(days_list),
    }
