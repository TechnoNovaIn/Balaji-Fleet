"""Self-learning tonnage refinements (fully local, offline).

No cloud AI, no weighbridge labels. The system learns from the data that flows
in daily:

1. LOADED/EMPTY CLASSIFIER (LightGBM)
   - Labels: the truck's own `vehicle_load_cond` flag ('01'=empty, '10'=loaded)
     from the 197 vehicles that emit it.
   - Features: per-window statistics of speed/rpm/engine_load/fuel_rate/
     accel_pedal/gear + derived power / load-per-rpm / accel.
   - After training it generalises to ALL trucks (even ones without the flag),
     so empty windows can be found for tare calibration fleet-wide.

2. PER-FAMILY CALIBRATION FACTOR (replaces the blanket 2.0)
   - Anchor: RTO/spec sheet kerb weight (vehicles.kerb_weight_kg) = the true
     mass of the truck running EMPTY.
   - For each truck with a kerb weight and empty-flagged windows: run the raw
     (unscaled) physics solver on those empty windows and solve
     factor = kerb_weight / solver_mass_empty.
   - Per-vehicle factors are aggregated per family (vehicle_type) and a global
     fallback is the fleet median. The estimate_trip batch applies the learned
     factor so absolute total_mass_kg / payload_tonnes are in true kg.

Artifacts (ships inside the repo, git-tracked):
   models/loaded_empty_lgbm.txt   LightGBM text dump
   models/calibration.json        {"families": {...}, "global": X, "meta": {...}}
"""

import json
import statistics
from datetime import datetime, timedelta

import numpy as np
import lightgbm as lgb

from app.db import connect
from app.tonnage import (_windows_from_rows, _clean, _pooled_masses,
                         _robust_estimate, mass_from_energy_balance,
                         TONNAGE_CALIBRATION_FACTOR)

MODELS_DIR = None  # set by load/store helpers via config

FEATURES = [
    "n", "dur_s",
    "speed_mean", "speed_med", "speed_std", "speed_p90", "speed_max",
    "speed_cv", "cruise_frac",
    "rpm_mean", "rpm_med", "rpm_std", "rpm_p90",
    "load_mean", "load_med", "load_std", "load_p90",
    "fuel_mean", "fuel_med", "fuel_std", "fuel_sum", "fuel_per_km",
    "accel_mean", "accel_med", "throttle_off_frac", "full_throttle_frac",
    "dv_accel_mean", "dv_decel_mean", "brake_frac", "hard_brake_count",
    "stop_count", "gear_change_rate",
    "gear_mean", "gear_med",
    "load_per_rpm", "fuel_per_speed", "power_mean", "accel_dv_mean",
    "physics_mass_t",
]

# behaviour thresholds used by window_features (speed in km/h, decel in m/s^2)
MOVING_SPEED = 2.0
BRAKE_MS2 = 0.5
HARD_BRAKE_MS2 = 1.2
THROTTLE_OFF_MAX = 2.0
FULL_THROTTLE_MIN = 90.0
CRUISE_BAND_KMH = 3.0

CLASSIFIER_PATH = "loaded_empty_lgbm.txt"
CALIBRATION_PATH = "calibration.json"


def _models_dir():
    global MODELS_DIR
    if MODELS_DIR is None:
        from app.config import BASE_DIR
        MODELS_DIR = BASE_DIR / "models"
    return MODELS_DIR


def _f(arr, fn, default=None):
    vals = [x for x in arr if x is not None]
    if not vals:
        return default
    try:
        return fn(vals)
    except (ValueError, IndexError, ZeroDivisionError):
        return default


def window_features(rows, factor=None):
    """Feature dict for one driving window (list of telemetry dicts).

    If `factor` (calibration multiplier) is given, the physics_mass_t feature is
    anchored on the calibrated absolute kg scale — this lets the payload
    regressor read absolute mass instead of re-deriving it.
    """
    speed = [r.get("speed") for r in rows]
    rpm = [r.get("rpm") for r in rows]
    load = [r.get("engine_load") for r in rows]
    fuel = [r.get("fuel_rate") for r in rows]
    accel_pedal = [r.get("accel_pedal") for r in rows]
    gear = [r.get("gear") for r in rows]

    dv, decel, accel = [], [], []
    for i in range(1, len(rows)):
        a, b = rows[i - 1].get("speed"), rows[i].get("speed")
        if a is not None and b is not None:
            d = (b - a) / 3.6 / 5.0
            dv.append(abs(d))
            if d < 0:
                decel.append(d)
            elif d > 0:
                accel.append(d)

    # stop-start events: crossing from below MOVING_SPEED to above it
    stop_count = sum(
        1 for i in range(1, len(rows))
        if (rows[i - 1].get("speed") or 0) < MOVING_SPEED and (rows[i].get("speed") or 0) >= MOVING_SPEED
    )

    # gear-change rate over consecutive rows
    gear_changes = 0
    gear_pairs = 0
    for i in range(1, len(rows)):
        ga, gb = rows[i - 1].get("gear"), rows[i].get("gear")
        if ga and gb:
            gear_pairs += 1
            if ga != gb:
                gear_changes += 1

    # steady-cruise fraction: moving rows within CRUISE_BAND_KMH of the median speed
    speed_med = _f(speed, statistics.median)
    cruise = 0
    moving_n = 0
    for v in speed:
        if v is None:
            continue
        if v >= MOVING_SPEED:
            moving_n += 1
            if speed_med is not None and abs(v - speed_med) <= CRUISE_BAND_KMH:
                cruise += 1

    n_trans = len(dv)
    throttle_vals = [x for x in accel_pedal if x is not None]
    n_thr = len(throttle_vals)

    power = [r["fuel_rate"] / 3600.0 * 845.0 * 42.6e6 * 0.40 for r in rows if r.get("fuel_rate")]

    f = {
        "n": len(rows),
        "dur_s": len(rows) * 5.0,
        "speed_mean": _f(speed, statistics.mean),
        "speed_med": speed_med,
        "speed_std": _f(speed, statistics.pstdev),
        "speed_p90": _f(speed, lambda v: sorted(v)[int(0.9 * (len(v) - 1))]),
        "speed_max": _f(speed, max),
        "speed_cv": None,
        "cruise_frac": round(cruise / moving_n, 4) if moving_n else None,
        "rpm_mean": _f(rpm, statistics.mean),
        "rpm_med": _f(rpm, statistics.median),
        "rpm_std": _f(rpm, statistics.pstdev),
        "rpm_p90": _f(rpm, lambda v: sorted(v)[int(0.9 * (len(v) - 1))]),
        "load_mean": _f(load, statistics.mean),
        "load_med": _f(load, statistics.median),
        "load_std": _f(load, statistics.pstdev),
        "load_p90": _f(load, lambda v: sorted(v)[int(0.9 * (len(v) - 1))]),
        "fuel_mean": _f(fuel, statistics.mean),
        "fuel_med": _f(fuel, statistics.median),
        "fuel_std": _f(fuel, statistics.pstdev),
        "fuel_sum": _f(fuel, sum),
        "accel_mean": _f(accel_pedal, statistics.mean),
        "accel_med": _f(accel_pedal, statistics.median),
        "throttle_off_frac": (round(sum(1 for x in throttle_vals if x < THROTTLE_OFF_MAX) / n_thr, 4)
                              if n_thr else None),
        "full_throttle_frac": (round(sum(1 for x in throttle_vals if x >= FULL_THROTTLE_MIN) / n_thr, 4)
                               if n_thr else None),
        "dv_accel_mean": _f(accel, statistics.mean),
        "dv_decel_mean": _f(decel, statistics.mean),
        "brake_frac": round(sum(1 for d in decel if d <= -BRAKE_MS2) / n_trans, 4) if n_trans else None,
        "hard_brake_count": sum(1 for d in decel if d <= -HARD_BRAKE_MS2),
        "stop_count": stop_count,
        "gear_change_rate": round(gear_changes / gear_pairs, 4) if gear_pairs else None,
        "gear_mean": _f(gear, statistics.mean),
        "gear_med": _f(gear, statistics.median),
        "accel_dv_mean": _f(dv, statistics.mean),
    }
    mean_speed = f["speed_mean"]
    mean_rpm = f["rpm_mean"]
    if f["speed_mean"] and f["speed_std"]:
        f["speed_cv"] = round(f["speed_std"] / f["speed_mean"], 4)
    f["load_per_rpm"] = (f["load_mean"] / mean_rpm * 1000.0
                          if f["load_mean"] is not None and mean_rpm else None)
    f["fuel_per_speed"] = (f["fuel_mean"] / mean_speed
                           if f["fuel_mean"] is not None and mean_speed else None)
    f["power_mean"] = statistics.mean(power) if power else None
    # fuel_per_km: load-invariant consumption (L/100km-ish) — normalises out speed
    f["fuel_per_km"] = (f["fuel_mean"] / (mean_speed / 3.6) * 1000.0
                        if f["fuel_mean"] is not None and mean_speed else None)
    # physics_mass_t: calibrated physics mass estimate for this window's mass,
    # anchors the regressor to absolute kg so it doesn't have to re-derive scale
    f["physics_mass_t"] = _window_physics_mass(rows) if factor is None else _window_physics_mass(rows, factor=factor)
    return f


def _feature_row(feat):
    return [feat.get(k) for k in FEATURES]


def _window_physics_mass(rows, factor=None):
    """Calibrated physics mass estimate (tonnes) for one window.

    Pools the energy-balance + accel + cruise estimators with the per-family
    calibration factor so the value is on the same absolute kg scale as the
    shipment payload target. Returns None if the window is kinematically
    degenerate (no acceleration / too slow).
    """
    from app.tonnage import _clean, _pooled_masses
    clean = _clean(rows)
    if len(clean) < 4:
        return None
    masses, _energy, _accel, _cruise, _windows = _pooled_masses(clean)
    if not masses:
        return None
    med = float(np.median(masses))
    if factor is not None and factor > 0:
        med *= factor
    return med / 1000.0  # kg -> tonnes


def _window_label(rows):
    """1 = loaded, 0 = empty, None = no clear majority flag."""
    c10 = sum(1 for r in rows if r.get("vehicle_load_cond") == "10")
    c01 = sum(1 for r in rows if r.get("vehicle_load_cond") == "01")
    total = c10 + c01
    if total < 4:
        return None
    if c10 >= 0.6 * total:
        return 1
    if c01 >= 0.6 * total:
        return 0
    return None


def sliding_windows(clean, size=24, step=6):
    """Sliding fixed-length windows (size rows @5s, step rows) for ML features.

    The coarse trip splitter (_windows_from_rows) yields few windows per day;
    for learning we want many overlapping windows, each long enough to see a
    stable load pattern.
    """
    if len(clean) < size:
        return [clean]
    out = []
    for i in range(0, len(clean) - size + 1, step):
        out.append(clean[i:i + size])
    return out


def build_classifier_dataset(days=7, min_rows_per_vehicle=30, verbose=True):
    """Pull flagged telemetry, split into windows, return (X, y, meta).

    X: list of feature rows aligned to FEATURES. y: 1=loaded, 0=empty.
    meta: list of dicts with vehicle_no / time for debugging.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT v.intangle_id, v.vehicle_no
                FROM vehicles v
                JOIN telemetry t ON t.vehicle_id = v.intangle_id
                WHERE v.is_active
                  AND t.time > now() - make_interval(days => %s)
                  AND t.vehicle_load_cond IN ('01','10')
                ORDER BY v.vehicle_no
                """,
                (days,),
            )
            plates = [r["vehicle_no"] for r in cur.fetchall()]

        X, y, meta = [], [], []
        for plate in plates:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT time, speed, rpm, gear, fuel_rate, engine_load,
                           accel_pedal, vehicle_load_cond
                    FROM telemetry t
                    JOIN vehicles v ON v.intangle_id = t.vehicle_id
                    WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
                      AND t.speed > 2 AND t.rpm > 0 AND t.fuel_rate > 0
                    ORDER BY t.time
                    """,
                    (plate, days),
                )
                rows = cur.fetchall()
            if len(rows) < min_rows_per_vehicle:
                continue
            clean = _clean([dict(r) for r in rows])
            for w in sliding_windows(clean):
                label = _window_label(w)
                if label is None:
                    continue
                feat = window_features(w)
                X.append(_feature_row(feat))
                y.append(label)
                meta.append({"vehicle_no": plate, "time": w[0].get("time")})
        conn.commit()

    if verbose:
        print(f"classifier dataset: {len(X)} windows, "
              f"loaded={sum(y)}, empty={len(y) - sum(y)}, from {len(plates)} vehicles")
    return X, y, meta


def train_classifier(X, y, out_path=None, verbose=True):
    """Train + cross-validate a LightGBM binary classifier. Saves to out_path."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    from sklearn.model_selection import StratifiedKFold
    from sklearn.metrics import accuracy_score, roc_auc_score

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.06,
        "num_leaves": 31,
        "max_depth": 6,
        "min_child_samples": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
    }
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros(len(X))
    models = []
    for tr_idx, va_idx in skf.split(X, y):
        dtr = lgb.Dataset(X[tr_idx], label=y[tr_idx])
        dva = lgb.Dataset(X[va_idx], label=y[va_idx], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=300, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(30, verbose=False),
                                 lgb.log_evaluation(0)])
        models.append(m)
        oof[va_idx] = m.predict(X[va_idx], num_iteration=m.best_iteration)
    acc = accuracy_score(y, oof > 0.5)
    auc = roc_auc_score(y, oof)
    if verbose:
        print(f"classifier CV: acc={acc:.3f} auc={auc:.3f}")

    best = models[int(np.argmax([m.best_score["valid_0"]["auc"] for m in models]))]
    if out_path:
        path = _models_dir() / out_path
        path.parent.mkdir(parents=True, exist_ok=True)
        best.save_model(str(path))
        if verbose:
            print(f"saved classifier -> {path}")
    imp = sorted(zip(FEATURES, best.feature_importance("gain")), key=lambda x: -x[1])[:10]
    if verbose:
        print("top features:", ", ".join(f"{k}={v:.0f}" for k, v in imp))
    return {"accuracy": round(acc, 3), "auc": round(auc, 3), "oof": oof, "model": best}


def load_classifier():
    path = _models_dir() / CLASSIFIER_PATH
    if not path.exists():
        return None
    return lgb.Booster(model_file=str(path))


def classify_windows_prob(windows, model):
    """Prob(loaded) for each window (list of row-lists)."""
    if model is None:
        return None
    X = np.asarray([_feature_row(window_features(w)) for w in windows], dtype=float)
    return model.predict(X)


def _vehicle_family(vehicle_type):
    if not vehicle_type:
        return "unknown"
    t = vehicle_type.lower()
    if "trailer" in t:
        return "trailer"
    if "truck" in t or "tractor" in t or "rigid" in t:
        return "truck"
    return t.strip() or "unknown"


def learn_calibration_factors(days=7, min_empty_rows=30, min_factor_samples=5,
                              min_family_samples=2, classifier=None,
                              empty_prob_threshold=0.25, verbose=True):
    """Per-family + global calibration factors anchored on RTO kerb weight.

    For each vehicle with vehicles.kerb_weight_kg:
      factor = (kerb + trailer_weight) / raw-solver-mass(EMPTY driving windows)
    The solver measures TOTAL combination mass, so the anchor must include the
    trailer weight for trailers (14W=6000kg, 18W=8000kg), mirroring how
    estimate_trip adds trailer_weight to tare.

    Trusted anchors come from the ECU's own `vehicle_load_cond='01'` (empty)
    flag -- ground truth, no weighbridge. The loaded/empty ML classifier
    (trained on the flag trucks) can EXPAND the pool to trucks without the
    flag, but only anchors whose factor is within a band around the flag-derived
    median are admitted, because classifier-selected "empty" windows skew
    toward light-throttle/coasting where the physics solver under-reads mass.
    Factors grouped by family (vehicle_type); global fallback = fleet median
    of the trusted pool. Writes JSON.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (v.intangle_id) v.intangle_id, v.vehicle_no,
                       v.kerb_weight_kg, v.vehicle_type, v.trailer_weight_kg
                FROM vehicles v
                WHERE v.is_active AND v.kerb_weight_kg IS NOT NULL AND v.kerb_weight_kg > 0
                ORDER BY v.intangle_id, v.vehicle_no
                """,
            )
            anchored = cur.fetchall()

        per_vehicle = []
        for row in anchored:
            plate = row["vehicle_no"]
            kerb = row["kerb_weight_kg"]
            trailer = row["trailer_weight_kg"] or 0
            anchor_mass = kerb + trailer
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT time, speed, rpm, gear, fuel_rate, engine_load,
                           accel_pedal, vehicle_load_cond
                    FROM telemetry t
                    JOIN vehicles v ON v.intangle_id = t.vehicle_id
                    WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
                      AND t.speed > 2 AND t.rpm > 0 AND t.fuel_rate > 0
                    ORDER BY t.time
                    """,
                    (plate, days),
                )
                rows = cur.fetchall()
            if len(rows) < min_empty_rows:
                continue
            clean = _clean([dict(r) for r in rows])
            flag_empty = [
                r for r in clean
                if r.get("vehicle_load_cond") == "01"
                and r.get("speed", 0) > 10 and r.get("rpm", 0) > 400
            ]
            if len(flag_empty) >= min_empty_rows:
                empty_rows, source = flag_empty, "flag"
            elif classifier is not None:
                probs = classify_windows_prob(sliding_windows(clean), classifier)
                empty_rows = [
                    r for w, p in zip(sliding_windows(clean), probs) if p < empty_prob_threshold
                    for r in w
                ]
                source = "ml"
            else:
                empty_rows, source = [], "flag"
            if len(empty_rows) < min_empty_rows:
                continue
            masses, *_ = _pooled_masses(empty_rows, calibration_factor=1.0)
            if len(masses) < 4:
                continue
            est = _robust_estimate(masses)
            if not (2000 < est < 60000):
                continue
            factor = anchor_mass / est
            if 0.3 < factor < 6.0:
                per_vehicle.append({
                    "vehicle_no": plate,
                    "family": _vehicle_family(row["vehicle_type"]),
                    "vehicle_type": row["vehicle_type"],
                    "kerb": kerb,
                    "trailer_weight": trailer,
                    "anchor_mass": round(anchor_mass),
                    "raw_mass": round(est),
                    "factor": round(factor, 3),
                    "windows": len(masses),
                    "empty_source": source,
                })

        conn.commit()

    trusted = [pv for pv in per_vehicle if pv["empty_source"] == "flag"]
    if len(trusted) < min_factor_samples:
        if verbose:
            print(f"calibration: only {len(trusted)} flag-anchored vehicles, "
                  f"need >= {min_factor_samples}; keeping blanket factor")
        return None

    flag_med = statistics.median([pv["factor"] for pv in trusted])
    band_lo, band_hi = flag_med * 0.6, flag_med * 1.7
    pool = list(trusted)
    expanded = 0
    for pv in per_vehicle:
        if pv["empty_source"] == "ml" and band_lo <= pv["factor"] <= band_hi:
            pool.append(pv)
            expanded += 1

    by_family = {}
    for pv in pool:
        by_family.setdefault(pv["family"], []).append(pv["factor"])
    families = {
        fam: round(statistics.median(vals), 3)
        for fam, vals in by_family.items() if len(vals) >= min_family_samples
    }
    global_factor = round(statistics.median([pv["factor"] for pv in pool]), 3)
    data = {
        "families": families,
        "global": global_factor,
        "meta": {
            "trained_at": datetime.now(timezone_utc()).isoformat(),
            "days": days,
            "n_vehicles": len(pool),
            "n_trusted": len(trusted),
            "n_ml_expanded": expanded,
            "old_blanket": TONNAGE_CALIBRATION_FACTOR,
        },
        "vehicles": pool,
    }
    path = _models_dir() / CALIBRATION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    if verbose:
        print(f"calibration: global={global_factor}, families={families}, "
              f"trusted={len(trusted)}, ml_expanded={expanded} -> {path}")
    return data


def _window_raw_mass(w):
    """Raw (unscaled) total-mass estimate for one window, reused across learn paths."""
    est = mass_from_energy_balance(w)
    if est:
        return est
    return None


def timezone_utc():
    from datetime import timezone
    return timezone.utc


def load_calibration():
    path = _models_dir() / CALIBRATION_PATH
    if not path.exists():
        return None
    return json.loads(path.read_text())


def calibration_factor_for(vehicle_type, calibration=None):
    """Learned factor for a vehicle (by family), else global, else blanket."""
    calibration = calibration or load_calibration()
    if not calibration:
        return TONNAGE_CALIBRATION_FACTOR
    fam = _vehicle_family(vehicle_type)
    return calibration.get("families", {}).get(fam, calibration.get("global", TONNAGE_CALIBRATION_FACTOR))


_model_cache = {"classifier": None, "calibration": None}


def get_models(refresh=False):
    """Lazily load the trained classifier + calibration once per process."""
    if refresh:
        _model_cache["classifier"] = load_classifier()
        _model_cache["calibration"] = load_calibration()
    if _model_cache["classifier"] is None:
        _model_cache["classifier"] = load_classifier()
    if _model_cache["calibration"] is None:
        _model_cache["calibration"] = load_calibration()
    return _model_cache["classifier"], _model_cache["calibration"]
