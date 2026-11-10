"""Supervised payload regression + load/unload event detection.

Ground truth now exists: the client's shipment manifest (data/shipments.csv ->
`shipments` table) gives NET WEIGHT (tonnes) per (vehicle, day). This module
trains a LightGBM regressor that maps a driving window's features to payload
tonnes directly, using the shipment weights as labels:

  - For each (vehicle, day) with a shipment weight W:
      * pull that day's telemetry, clean it, slide fixed windows
      * run the trained loaded/empty classifier; windows it scores as clearly
        LOADED get label W, clearly EMPTY get label 0, ambiguous are dropped
  - Train LightGBM regression (window features -> payload tonnes).
  - Save models/payload_regressor.txt

Then `load_events(vehicle_no, day)` runs the regressor over every window of a
day, smooths the resulting payload-vs-time curve, and emits LOADED/UNLOADED
events wherever the payload crosses a threshold -- so the admin sees exactly
when a truck was loaded and when it was emptied.

Fully local. No cloud AI.
"""

import json
import statistics
from datetime import datetime, timedelta, timezone

import numpy as np
import lightgbm as lgb

from app.db import connect
from app.ml_tonnage import (FEATURES, _feature_row, load_classifier, get_models,
                            sliding_windows, window_features, _clean,
                            _window_physics_mass, load_calibration,
                            calibration_factor_for)
from app.tonnage import _pooled_masses, _robust_estimate

REGRESSOR_PATH = "payload_regressor.txt"
EVENT_PATH = "load_events.json"

LOADED_PROB_MIN = 0.65   # window must score >= this to be labelled loaded
EMPTY_PROB_MAX = 0.35    # window must score <= this to be labelled empty
EVENT_THRESHOLD_T = 10.0  # payload above this = loaded, below = empty (event detection)
HYSTERESIS_T = 4.0       # stay loaded until payload drops threshold-hysteresis, and vice versa


def _models_dir():
    from app.ml_tonnage import _models_dir as m
    return m()


def _payload_for_day(plate, day):
    """Clean telemetry rows for one vehicle+day (list of dicts)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT time, speed, rpm, gear, fuel_rate, engine_load,
                       accel_pedal, vehicle_load_cond
                FROM telemetry t
                JOIN vehicles v ON v.intangle_id = t.vehicle_id
                WHERE v.vehicle_no = %s
                  AND to_char(t."time" AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD') = %s
                ORDER BY t."time"
                """,
                (plate, day),
            )
            rows = [dict(r) for r in cur.fetchall()]
    return _clean(rows)


def _telemetry_days():
    """Set of (vehicle_no, day) pairs that have telemetry (IST day)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.vehicle_no,
                       (t."time" AT TIME ZONE 'Asia/Kolkata')::date AS day
                FROM telemetry t
                JOIN vehicles v ON v.intangle_id = t.vehicle_id
                GROUP BY 1, 2
                """
            )
            return {(r["vehicle_no"], str(r["day"])) for r in cur.fetchall()}


def build_regression_dataset(days_back=365, min_windows=8, verbose=True):
    """Build (X, y, meta) for supervised payload regression.

    X: feature rows aligned to FEATURES. y: payload tonnes (0 for empty).
    meta: per-window {vehicle_no, time, source}.
    Labels come from shipment weights; loaded/empty split from the classifier.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.vehicle_no, s.shipment_date, s.net_weight_t
                FROM shipments s
                WHERE s.net_weight_t IS NOT NULL AND s.net_weight_t > 0
                  AND s.shipment_date >= (now() AT TIME ZONE 'Asia/Kolkata')::date - %s
                ORDER BY s.shipment_date
                """,
                (timedelta(days=days_back),),
            )
            labels = cur.fetchall()
    # one row per (vehicle, day): keep the heaviest shipment as the loaded target
    by_day = {}
    for r in labels:
        key = (r["vehicle_no"], str(r["shipment_date"]))
        if key not in by_day or r["net_weight_t"] > by_day[key]:
            by_day[key] = r["net_weight_t"]

    classifier, calibration = get_models()
    if classifier is None:
        classifier = load_classifier()

    telemetry_days = _telemetry_days()

    X, y, meta = [], [], []
    skipped_no_clf = 0
    # fetch vehicle_type so we can apply the per-family calibration factor
    # to the physics_mass_t feature (anchors the regressor to absolute tonnes)
    vtypes = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT vehicle_no, vehicle_type FROM vehicles WHERE vehicle_type IS NOT NULL")
            for r in cur.fetchall():
                vtypes[r["vehicle_no"]] = r["vehicle_type"]

    for (plate, day), weight in sorted(by_day.items()):
        if (plate, day) not in telemetry_days:
            continue
        rows = _payload_for_day(plate, day)
        if len(rows) < 30:
            continue
        wins = sliding_windows(rows, size=24, step=6)
        if len(wins) < min_windows:
            continue
        if classifier is None:
            # fallback: label every window by raw physics mass relative split
            skipped_no_clf += 1
            continue
        factor = None
        if calibration and plate in vtypes:
            factor = calibration_factor_for(vtypes[plate], calibration)
        frows = [_feature_row(window_features(w, factor=factor)) for w in wins]
        probs = classifier.predict(np.asarray(frows, dtype=float))
        for w, frow, p in zip(wins, frows, probs):
            if p >= LOADED_PROB_MIN:
                X.append(frow)
                y.append(weight)
                meta.append({"vehicle_no": plate, "time": w[0]["time"], "label": "loaded"})
            elif p <= EMPTY_PROB_MAX:
                X.append(frow)
                y.append(0.0)
                meta.append({"vehicle_no": plate, "time": w[0]["time"], "label": "empty"})

    if verbose:
        n_loaded = sum(1 for yy in y if yy > 0)
        n_anchor = sum(1 for m in meta if m["label"] == "anchor")
        print(f"regression dataset: {len(X)} windows, loaded={n_loaded}, "
              f"empty={len(y) - n_loaded}, "
              f"dwell-anchored={n_anchor} ({round(100 * n_anchor / max(1, len(X)), 1)}%), "
              f"vehicles={len(set(m['vehicle_no'] for m in meta))}, "
              f"from {len(by_day)} shipment-days")
        if skipped_no_clf:
            print(f"  (note: {skipped_no_clf} shipment-days skipped, no classifier)")
    return X, y, meta


def evaluate_vs_shipments(days_back=365, model=None, verbose=True):
    """Accuracy of the current regressor against the shipment manifest.

    The shipment net weights ARE the weighbridge-equivalent ground truth. For
    every (vehicle, day) with a shipment weight W, predict the day's loaded
    payload from OBD and compare. Returns fleet MAE (t) + R2 + the per-truck
    breakdown so the admin can see exactly which trucks the model nails and
    which it misses. Run by the self-training loop after each retrain so the
    accuracy trend is tracked over time.
    """
    import math
    from sklearn.metrics import mean_absolute_error, r2_score

    model = model if model is not None else load_regressor()
    if model is None:
        return None
    classifier, calibration = get_models()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_no, shipment_date, max(net_weight_t) AS weight
                FROM shipments
                WHERE net_weight_t IS NOT NULL AND net_weight_t > 0
                  AND shipment_date >= (now() AT TIME ZONE 'Asia/Kolkata')::date - %s
                GROUP BY vehicle_no, shipment_date
                """,
                (timedelta(days=days_back),),
            )
            labels = cur.fetchall()
    vtypes = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT vehicle_no, vehicle_type FROM vehicles WHERE vehicle_type IS NOT NULL")
            for r in cur.fetchall():
                vtypes[r["vehicle_no"]] = r["vehicle_type"]

    telemetry_days = _telemetry_days()

    actuals, preds, per = [], [], []
    for r in labels:
        plate, day, weight = r["vehicle_no"], str(r["shipment_date"]), r["weight"]
        if (plate, day) not in telemetry_days:
            continue
        rows = _payload_for_day(plate, day)
        if len(rows) < 30:
            continue
        wins = sliding_windows(rows, size=24, step=6)
        factor = None
        if calibration and plate in vtypes:
            factor = calibration_factor_for(vtypes[plate], calibration)
        pred = predict_windows_payload(wins, model, vehicle_no=plate, calibration=calibration)
        if pred is None:
            continue
        loaded = [p for p in pred if p > 5.0]  # payload during loaded legs
        if not loaded:
            continue
        est = float(np.median(loaded))
        actuals.append(weight)
        preds.append(est)
        per.append({"vehicle_no": plate, "day": day, "shipment_t": round(weight, 1),
                    "est_t": round(est, 1), "error_t": round(est - weight, 1)})

    if not actuals:
        return None
    mae = mean_absolute_error(actuals, preds)
    r2 = r2_score(actuals, preds) if len(actuals) > 2 else float("nan")
    if verbose:
        print(f"vs shipment manifest: MAE {mae:.2f}t, R2 {r2:.3f} over {len(actuals)} shipment-days")
    return {"mae_t": round(mae, 2), "r2": round(r2, 3) if not math.isnan(r2) else None,
            "n_shipments": len(actuals), "per_shipment": per}


def train_regressor(X, y, out_path=REGRESSOR_PATH, verbose=True):
    """Train + cross-validate LightGBM regressor. Saves to models/."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    from sklearn.model_selection import KFold
    from sklearn.metrics import mean_absolute_error, r2_score

    params = {
        "objective": "regression",
        "metric": "mae",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 6,
        "min_child_samples": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
    }
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros(len(X))
    models = []
    for tr_idx, va_idx in kf.split(X):
        dtr = lgb.Dataset(X[tr_idx], label=y[tr_idx])
        dva = lgb.Dataset(X[va_idx], label=y[va_idx], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=400,
                      valid_sets=[dva],
                      callbacks=[lgb.early_stopping(40, verbose=False),
                                 lgb.log_evaluation(0)])
        models.append(m)
        oof[va_idx] = m.predict(X[va_idx], num_iteration=m.best_iteration)

    mae = mean_absolute_error(y, oof)
    r2 = r2_score(y, oof)
    loaded = y > 5
    mae_loaded = mean_absolute_error(y[loaded], oof[loaded]) if loaded.any() else float("nan")
    mae_empty = mean_absolute_error(y[~loaded], oof[~loaded]) if (~loaded).any() else float("nan")
    if verbose:
        print(f"regressor CV: mae={mae:.2f}t r2={r2:.3f} "
              f"(loaded {mae_loaded:.2f}t, empty {mae_empty:.2f}t)")

    best = models[int(np.argmin([m.best_score["valid_0"]["l2"] if "l2" in m.best_score["valid_0"]
                                else list(m.best_score["valid_0"].values())[0]
                                for m in models]))]
    if out_path:
        path = _models_dir() / out_path
        path.parent.mkdir(parents=True, exist_ok=True)
        best.save_model(str(path))
        if verbose:
            print(f"saved regressor -> {path}")
    imp = sorted(zip(FEATURES, best.feature_importance("gain")), key=lambda x: -x[1])[:12]
    if verbose:
        print("top features:", ", ".join(f"{k}={v:.0f}" for k, v in imp))
    return {"mae": round(mae, 3), "r2": round(r2, 3), "oof": oof, "model": best}


def load_regressor():
    path = _models_dir() / REGRESSOR_PATH
    if not path.exists():
        return None
    return lgb.Booster(model_file=str(path))


def predict_windows_payload(wins, model, vehicle_no=None, calibration=None):
    """Payload tonnes per window (list of row-lists).

    If vehicle_no + calibration are supplied, the physics_mass_t feature is
    computed on the same calibrated scale as training.
    """
    if model is None:
        return None
    factor = None
    if vehicle_no and calibration:
        vtypes = _vehicle_types([vehicle_no])
        vtype = vtypes.get(vehicle_no)
        if vtype:
            factor = calibration_factor_for(vtype, calibration)
    X = np.asarray([_feature_row(window_features(w, factor=factor)) for w in wins], dtype=float)
    return model.predict(X)


_VTYPES = {}


def _vehicle_types(plates):
    """Cached vehicle_no -> vehicle_type lookup."""
    missing = [p for p in plates if p not in _VTYPES]
    if missing:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT vehicle_no, vehicle_type FROM vehicles WHERE vehicle_no = ANY(%s)",
                    (list(plates),),
                )
                for r in cur.fetchall():
                    _VTYPES[r["vehicle_no"]] = r["vehicle_type"]
    return {p: _VTYPES.get(p) for p in plates}


def smooth(vals, window=3):
    """Rolling median smoothing of a payload curve (list)."""
    if not vals:
        return []
    out = []
    for i in range(len(vals)):
        lo = max(0, i - window)
        hi = min(len(vals), i + window + 1)
        out.append(statistics.median(vals[lo:hi]))
    return out


def _win_mid(w):
    t = w[len(w) // 2]["time"]
    return t.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None) if t.tzinfo is None else t


def load_events(vehicle_no, day, model=None, threshold_t=EVENT_THRESHOLD_T,
                min_gap_minutes=30, verbose=False):
    model = model or load_regressor()
    rows = _payload_for_day(vehicle_no, day)
    if len(rows) < 12:
        return {"vehicle_no": vehicle_no, "day": day, "events": [], "curve": [],
                "error": "insufficient telemetry"}
    wins = sliding_windows(rows, size=24, step=6)
    calibration = load_calibration()
    pred = predict_windows_payload(wins, model, vehicle_no=vehicle_no, calibration=calibration)
    if pred is None:
        return {"vehicle_no": vehicle_no, "day": day, "events": [], "curve": [],
                "error": "no payload model trained"}
    payload = smooth([max(0.0, p) for p in pred])
    times = [_win_mid(w) for w in wins]

    events = []
    state = 0  # 0 = empty, 1 = loaded
    last_event = None
    for t, p in zip(times, payload):
        if state == 0:
            s = 1 if p > threshold_t else 0
        else:
            s = 1 if p > threshold_t - HYSTERESIS_T else 0
        if s != state:
            kind = "loaded" if s == 1 else "unloaded"
            if last_event is None or (t - last_event).total_seconds() / 60 >= min_gap_minutes:
                events.append({
                    "type": kind,
                    "time": t.isoformat(),
                    "payload_t": round(p, 1),
                })
                last_event = t
            state = s
    if state == 1:
        events.append({"type": "unloaded", "time": times[-1].isoformat(),
                       "payload_t": round(payload[-1], 1), "end_of_day": True})

    curve = [{"time": t.isoformat(), "payload_t": round(p, 1)}
             for t, p in zip(times, payload)]
    if verbose:
        print(f"{vehicle_no} {day}: {len(events)} events, {len(curve)} curve pts")
    return {"vehicle_no": vehicle_no, "day": day, "events": events, "curve": curve,
            "window_size_s": 24 * 5, "threshold_t": threshold_t, "model_ok": model is not None}


_regressor_cache = {"model": None, "events": None}


def get_regressor(refresh=False):
    if refresh:
        _regressor_cache["model"] = load_regressor()
    if _regressor_cache["model"] is None:
        _regressor_cache["model"] = load_regressor()
    return _regressor_cache["model"]
