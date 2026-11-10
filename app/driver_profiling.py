"""Driver profiling from OBD telemetry (fully local, no driver ID).

The telemetry has no driver identity -- a truck may be shared across shifts and
the OBD feed carries only vehicle data. So "who is driving" is inferred from
BEHAVIOUR: sliding windows of each vehicle's driving are scored for throttle
jerk, hard accel/brake, over-revving, gear-shift frequency, idling, speeding
and fuel economy, then clustered into driving STYLES. A driver profile is the
distribution of those styles plus fleet-benchmark percentiles.

Relationship to the tonnage pipeline:
  - Reads telemetry + vehicles only (read-only).
  - CONSUMES the trained loaded/empty classifier (app/ml_tonnage.py) to
    load-normalize behaviour, so a hard driver on an empty truck isn't confused
    with one on a full truck.
  - Never writes payload_tonnes / calibration / classifier artifacts. Driver
    profiling is a separate analysis layer (writes only to driver_profiles).

Confidence is data-driven (window count + cluster separation + freshness), not
a random number.
"""

import json
import statistics
from collections import Counter
from datetime import datetime, timezone

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from app.db import connect
from app.ml_tonnage import (sliding_windows, window_features,
                            classify_windows_prob)


FLEET_BENCHMARK_VEHICLES = 60      # cap for fleet percentile sampling
FLEET_BENCHMARK_WINDOWS = 300      # windows per sampled vehicle
MIN_WINDOWS_FOR_PROFILE = 12       # need at least this many windows to trust
WINDOW_SIZE = 24                   # rows @5s = 2 min
WINDOW_STEP = 6


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


def behavior_features(rows):
    """Extend the tonnage window features with driving-behaviour features.

    Returns a flat dict; None for features that can't be computed on the window
    (caller imputes). Rates are fractions (0-1) unless noted.
    """
    f = window_features(rows)
    n = len(rows)

    speeds = [r.get("speed") for r in rows]
    rpms = [r.get("rpm") for r in rows]
    gears = [r.get("gear") for r in rows]
    pedals = [r.get("accel_pedal") for r in rows]

    jerk = []
    for i in range(1, n):
        a, b = pedals[i - 1], pedals[i]
        if a is not None and b is not None:
            jerk.append(abs(b - a))
    f["throttle_jerk_mean"] = statistics.mean(jerk) if jerk else None

    dv = []
    for i in range(1, n - 1):
        a, b = speeds[i - 1], speeds[i + 1]
        if a is not None and b is not None:
            dv.append((b - a) / 3.6 / 10.0)
    f["hard_accel_rate"] = (sum(1 for x in dv if x > 1.5) / len(dv)) if dv else None
    f["harsh_brake_rate"] = (sum(1 for x in dv if x < -1.5) / len(dv)) if dv else None
    f["accel_mean"] = statistics.mean(dv) if dv else None
    f["accel_std"] = statistics.pstdev(dv) if len(dv) > 1 else None

    good_rpm = [r for r in rpms if r is not None]
    f["over_rev_rate"] = (sum(1 for r in good_rpm if r > 1900) / len(good_rpm)
                          if good_rpm else None)

    engaged = [g for g in gears if g is not None and g > 0]
    shifts = sum(1 for i in range(1, len(engaged)) if engaged[i] != engaged[i - 1])
    minutes = n * 5.0 / 60.0
    f["shift_rate_per_min"] = shifts / minutes if minutes else None

    good_sp = [s for s in speeds if s is not None]
    f["speeding_rate"] = (sum(1 for s in good_sp if s > 70) / len(good_sp)
                          if good_sp else None)

    kml = []
    for s, fr in zip(speeds, [r.get("fuel_rate") for r in rows]):
        if s and fr and s > 5 and fr > 0:
            kml.append(s / fr)
    f["km_per_l_mean"] = statistics.mean(kml) if kml else None
    return f


BEHAVIOUR_FEATURES = [
    "throttle_jerk_mean", "hard_accel_rate", "harsh_brake_rate",
    "over_rev_rate", "shift_rate_per_min", "speeding_rate",
    "accel_mean", "accel_std", "km_per_l_mean",
]


def _behavior_row(feat):
    return [feat.get(k) for k in BEHAVIOUR_FEATURES]


def _pull_windows(plate, days, max_windows=None):
    """Cleaned sliding windows for one vehicle, newest first (capped)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT time, speed, rpm, gear, fuel_rate, engine_load, accel_pedal
                FROM telemetry t
                JOIN vehicles v ON v.intangle_id = t.vehicle_id
                WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
                ORDER BY t.time
                """,
                (plate, days),
            )
            rows = cur.fetchall()
    clean = _light_clean(rows)
    if not clean:
        return []
    wins = sliding_windows(clean, size=WINDOW_SIZE, step=WINDOW_STEP)
    if max_windows:
        wins = wins[-max_windows:]
    return wins


def _impute_matrix(windows):
    """Behaviour feature matrix with per-column median imputation + standardize."""
    feats = [behavior_features(w) for w in windows]
    X = np.asarray(
        [[float(v) if v is not None else np.nan for v in _behavior_row(f)]
         for f in feats],
        dtype=float,
    )
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)          # all-NaN column -> 0
    X = np.where(np.isnan(X), med, X)
    std = X.std(axis=0)
    X = np.where(std == 0, 1.0, X)
    return StandardScaler().fit_transform(X), feats


def cluster_styles(X_scaled):
    """Cluster windows into driving styles with DBSCAN.

    Returns (labels, n_clusters). -1 = noise/outlier. A single-driver truck
    usually yields 1 dense cluster + noise; shared trucks yield several.
    """
    model = DBSCAN(eps=1.1, min_samples=max(3, min(6, len(X_scaled) // 8))).fit(X_scaled)
    labels = model.labels_
    counts = Counter(l for l in labels if l >= 0)
    if not counts:
        return labels, 0
    if max(counts.values()) < MIN_WINDOWS_FOR_PROFILE * 0.5:
        return labels, 0
    return labels, len(counts)


def load_breakdown(windows, classifier):
    """{'empty': f, 'loaded': f} from classifier P(loaded) per window."""
    if classifier is None or not windows:
        return {"empty": None, "loaded": None}
    probs = classify_windows_prob(windows, classifier)
    loaded = float(np.mean(probs > 0.5))
    return {"empty": round(1.0 - loaded, 3), "loaded": round(loaded, 3)}


def fleet_percentiles(plate, days, metric="km_per_l_mean",
                      cap_vehicles=FLEET_BENCHMARK_VEHICLES):
    """Distribution of a behaviour metric across the fleet (for benchmarking)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT v.vehicle_no
                FROM vehicles v
                JOIN telemetry t ON t.vehicle_id = v.intangle_id
                WHERE v.is_active AND t.time > now() - make_interval(days => %s)
                  AND v.vehicle_no <> %s
                ORDER BY v.vehicle_no
                LIMIT %s
                """,
                (days, plate, cap_vehicles),
            )
            plates = [r["vehicle_no"] for r in cur.fetchall()]
    vals = []
    for p in plates:
        for w in _pull_windows(p, days, FLEET_BENCHMARK_WINDOWS):
            v = behavior_features(w).get(metric)
            if v is not None:
                vals.append(v)
        if len(vals) > FLEET_BENCHMARK_WINDOWS * cap_vehicles:
            break
    return sorted(vals)


def percentile_rank(value, dist):
    if value is None or not dist:
        return None
    return round(100.0 * sum(1 for x in dist if x <= value) / len(dist), 1)


def detect_anomalies(windows, X_scaled, feats, contamination=0.05):
    """IsolationForest over behaviour features -> most anomalous windows."""
    if len(windows) < 20:
        return []
    iso = IsolationForest(contamination=contamination, random_state=42).fit(X_scaled)
    preds = iso.predict(X_scaled)
    n_anom = int(preds.sum())
    if n_anom < 1:
        return []
    scores = iso.score_samples(X_scaled)
    idx = np.argsort(scores)[:min(8, n_anom)]
    out = []
    for i in idx:
        r0 = windows[i][0]
        out.append({
            "time": r0.get("time").isoformat() if r0.get("time") else None,
            "score": round(float(scores[i]), 2),
            "fuel_rate": round(r0.get("fuel_rate") or 0, 1),
            "rpm": r0.get("rpm"),
            "speed": r0.get("speed"),
        })
    return out


def idle_fraction(plate, days):
    """Day-level idle fraction: engine-on-but-stopped / engine-on rows."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE speed < 2)::float /
                       NULLIF(count(*), 0) AS frac
                FROM telemetry t
                JOIN vehicles v ON v.intangle_id = t.vehicle_id
                WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
                  AND rpm > 0
                """,
                (plate, days),
            )
            row = cur.fetchone()
    return row["frac"] if row and row["frac"] is not None else None


def profile_vehicle(plate, days=7, classifier=None):
    """Compute a full driver profile for a vehicle from its telemetry.

    Returns dict with driving_patterns (frontend list), profile_data (JSON),
    and confidence (0-1). Raise ValueError when there is not enough data.
    """
    windows = _pull_windows(plate, days)
    if len(windows) < MIN_WINDOWS_FOR_PROFILE:
        raise ValueError(
            f"not enough telemetry for {plate} in the last {days}d "
            f"({len(windows)} windows < {MIN_WINDOWS_FOR_PROFILE})")

    X_scaled, feats = _impute_matrix(windows)
    labels, n_clusters = cluster_styles(X_scaled)
    anomaly_frac = float(np.mean([l == -1 for l in labels])) if n_clusters else 0.0

    lb = load_breakdown(windows, classifier)

    def agg(metric):
        v = [f[metric] for f in feats if f.get(metric) is not None]
        return statistics.mean(v) if v else None

    patterns = []
    for label, metric, scale in [
        ("Hard Acceleration", "hard_accel_rate", 100.0),
        ("Harsh Braking", "harsh_brake_rate", 100.0),
        ("Over-revving", "over_rev_rate", 100.0),
        ("Speeding", "speeding_rate", 100.0),
        ("Throttle Jerk", "throttle_jerk_mean", 5.0),
        ("Gear Shifting", "shift_rate_per_min", 1.0),
    ]:
        v = agg(metric)
        if v is None:
            continue
        peak = max((f[metric] or 0) for f in feats)
        patterns.append({
            "type": label,
            "value": round(min(100.0, v * scale), 1),
            "samples": len(windows),
            "peak": round(min(100.0, peak * scale), 1),
        })

    idle = idle_fraction(plate, days)
    if idle is not None:
        patterns.append({
            "type": "Idling",
            "value": round(idle * 100.0, 1),
            "samples": len(windows),
            "peak": round(max(100.0, idle * 100.0), 1),
        })

    anomalies = detect_anomalies(windows, X_scaled, feats)

    kml = agg("km_per_l_mean")
    fuel_dist = fleet_percentiles(plate, days)
    fuel_pct = percentile_rank(kml, fuel_dist)

    # confidence: data volume + cluster separation + classifier coverage
    conf = 0.35 + 0.02 * min(30, len(windows))
    if n_clusters >= 2:
        conf += 0.08
    if lb.get("loaded") is not None:
        conf += 0.05
    conf = round(min(conf, 0.98), 2)

    profile_data = {
        "summary": {
            "windows": len(windows),
            "kmh_avg": round(agg("speed_mean") or 0, 1),
            "styles": n_clusters if n_clusters else 1,
            "anomaly_windows": len(anomalies),
        },
        "load_breakdown": lb,
        "fleet_fuel_percentile": fuel_pct,
        "fleet_fuel_samples": len(fuel_dist) if fuel_dist else 0,
        "km_per_l": round(kml, 2) if kml else None,
        "shift_rate_per_min": round(agg("shift_rate_per_min") or 0, 2),
        "anomalies": anomalies,
    }
    return {"patterns": patterns, "profile_data": profile_data, "confidence": conf}


def retrain_all_profiles(days=7, classifier=None):
    """Re-train every driver profile that has a vehicle_no.

    Returns {'updated': [...], 'skipped': [...]}. Profiles with too little
    telemetry keep their last trained results (skipped, not wiped).
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, driver_name, vehicle_no FROM driver_profiles "
                        "WHERE vehicle_no IS NOT NULL AND vehicle_no <> '' ORDER BY id")
            profiles = cur.fetchall()
    updated, skipped = [], []
    for p in profiles:
        try:
            result = profile_vehicle(p["vehicle_no"], days=days, classifier=classifier)
        except ValueError as e:
            skipped.append({"id": p["id"], "vehicle_no": p["vehicle_no"], "reason": str(e)})
            continue
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE driver_profiles SET trained_at = now(), confidence = %s, "
                    "driving_patterns = %s, profile_data = %s, updated_at = now() "
                    "WHERE id = %s",
                    (result["confidence"], json.dumps(result["patterns"]),
                     json.dumps(result["profile_data"]), p["id"]),
                )
            conn.commit()
        updated.append({"id": p["id"], "vehicle_no": p["vehicle_no"]})
    return {"updated": updated, "skipped": skipped}
