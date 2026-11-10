"""Self-training loop: keeps the tonnage models AND driver profiles fresh.

Runs forever in the background. Each pass:
  1. Retrains the loaded/empty classifier + per-family calibration factors
     (app/ml_tonnage.py) from the last N days of live telemetry.
  2. Re-trains every driver profile (app/driver_profiling.py) so behaviour
     fingerprints track new data.
  3. POSTs /ingest/reload-models to the running API so it picks up the new
     artifacts without a restart (same JWT secret, so the loop mints its own
     admin token).
  Optionally re-runs the tonnage batch (--with-tonnage) so the new factors are
  applied to cached payload estimates.

Driver profiling NEVER writes tonnage artifacts; this loop only re-trains
profiles and re-trains the tonnage models as two independent steps.

Usage:
    nohup .venv/bin/python -u -m scripts.train_loop --interval-hours 6 > train.log 2>&1 & disown
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib import request, error as urlerror

from app.config import settings
from app.db import BASE_DIR
from app.ml_tonnage import get_models
from app.security import create_token

from scripts.train_tonnage import run as retrain_tonnage
from app.driver_profiling import retrain_all_profiles


REGRESSION_DAYS_BACK = 365  # full weighbridge history (1 year) for the supervised regressor


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def reload_api_models(api_base, username="train-loop"):
    """POST /ingest/reload-models on the running API with a fresh admin token."""
    token = create_token(username, "admin")
    req = request.Request(
        f"{api_base}/ingest/reload-models",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (urlerror.URLError, OSError) as e:
        return {"error": str(e)}


def run_tonnage_batch(days):
    """Re-apply learned factors to cached payload estimates."""
    import subprocess
    res = subprocess.run(
        [sys.executable, "-m", "scripts.tonnage", "--days", str(days)],
        capture_output=True, text=True, timeout=3600,
    )
    return (res.stdout or res.stderr)[-400:]


def one_pass(days, with_tonnage, api_base):
    print(f"[{_now()}] === train pass start (days={days}) ===", flush=True)

    # 1. tonnage models
    try:
        classifier, calibration = retrain_tonnage(days=days)
        fam = calibration.get("families", {}) if calibration else {}
        print(f"[{_now()}] tonnage: classifier={'ok' if classifier else 'none'}, "
              f"global={calibration.get('global') if calibration else 'n/a'}, "
              f"families={fam}", flush=True)
    except Exception as e:
        classifier = None
        print(f"[{_now()}] tonnage retrain FAILED: {e}", flush=True)

    # 1b. supervised payload regressor (shipment ground truth) -- optional if classifier missing
    acc_history_path = BASE_DIR / "models" / "accuracy_history.json"
    try:
        from app.tonnage_regression import (build_regression_dataset, train_regressor,
                                            evaluate_vs_shipments)
        X, y, meta = build_regression_dataset(days_back=days, verbose=False)
        if len(X) >= 50:
            res = train_regressor(X, y, verbose=False)
            # honest self-evaluation: how close is the model to the shipment
            # (weighbridge-equivalent) net weights right after retraining?
            acc = evaluate_vs_shipments(days_back=days, model=res["model"], verbose=False)
            if acc:
                record = {"ts": _now(), "cv_mae_t": res["mae"], "cv_r2": res["r2"],
                          "manifest_mae_t": acc["mae_t"], "manifest_r2": acc["r2"],
                          "n_shipments": acc["n_shipments"], "n_windows": len(X)}
                history = []
                if acc_history_path.exists():
                    try:
                        history = json.loads(acc_history_path.read_text())
                    except Exception:
                        history = []
                history.append(record)
                acc_history_path.parent.mkdir(parents=True, exist_ok=True)
                acc_history_path.write_text(json.dumps(history[-120:], indent=2))
                print(f"[{_now()}] payload regressor: mae={res['mae']}t r2={res['r2']} "
                      f"({len(X)} windows); vs shipment manifest MAE {acc['mae_t']}t "
                      f"R2 {acc['r2']} over {acc['n_shipments']} shipment-days", flush=True)
            else:
                print(f"[{_now()}] payload regressor: mae={res['mae']}t r2={res['r2']} "
                      f"({len(X)} windows)", flush=True)
        else:
            print(f"[{_now()}] payload regressor: only {len(X)} windows, skipping", flush=True)
    except Exception as e:
        print(f"[{_now()}] payload regressor FAILED: {e}", flush=True)

    # 2. driver profiles (independent step, never touches tonnage artifacts)
    try:
        result = retrain_all_profiles(days=days, classifier=classifier)
        print(f"[{_now()}] driver profiles: {len(result['updated'])} updated, "
              f"{len(result['skipped'])} skipped", flush=True)
    except Exception as e:
        print(f"[{_now()}] driver profile retrain FAILED: {e}", flush=True)

    # 3. pick up new artifacts in the running API (no restart)
    try:
        reloaded = reload_api_models(api_base)
        print(f"[{_now()}] API reload: {reloaded}", flush=True)
    except Exception as e:
        print(f"[{_now()}] API reload FAILED: {e}", flush=True)

    # 4. optionally re-apply factors to cached payloads
    if with_tonnage:
        try:
            out = run_tonnage_batch(days)
            print(f"[{_now()}] tonnage batch applied: {out}", flush=True)
        except Exception as e:
            print(f"[{_now()}] tonnage batch FAILED: {e}", flush=True)

    print(f"[{_now()}] === train pass done ===", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-hours", type=float, default=6.0,
                        help="time between training passes")
    parser.add_argument("--days", type=int, default=365,
                        help="telemetry/shipment window (days) to train on")
    parser.add_argument("--with-tonnage", action="store_true",
                        help="also re-run the tonnage batch after retraining")
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit (for cron/testing)")
    args = parser.parse_args()

    if args.once:
        one_pass(args.days, args.with_tonnage, args.api_base)
        return

    while True:
        try:
            one_pass(args.days, args.with_tonnage, args.api_base)
        except Exception as e:
            print(f"[{_now()}] pass crashed: {e}", flush=True)
        print(f"[{_now()}] sleeping {args.interval_hours}h", flush=True)
        time.sleep(args.interval_hours * 3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("train loop interrupted")
        sys.exit(130)
