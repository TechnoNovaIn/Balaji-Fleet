"""Self-learning tonnage pipeline: retrain the loaded/empty classifier and the
per-family calibration factors from the last N days of live OBD telemetry.

No weighbridge data is used. Anchors:
  - loaded/empty labels: the truck's own ECU `vehicle_load_cond` flag
  - absolute mass: RTO/spec-sheet kerb weight (+ trailer weight for trailers)
The artifacts (models/loaded_empty_lgbm.txt, models/calibration.json) are
git-tracked and ship inside the repo; the tonnage batch and API use them.

Usage:
    python -m scripts.train_tonnage [--days N] [--skip-classifier]
    # then re-run the batch so new factors are applied:
    python -m scripts.tonnage --days 7
"""

import argparse
import sys
from datetime import datetime, timezone

from app.ml_tonnage import (build_classifier_dataset, train_classifier,
                            learn_calibration_factors, CLASSIFIER_PATH,
                            load_classifier, load_calibration)


def run(days=7, skip_classifier=False, min_factor_samples=5, min_family_samples=2):
    trained_at = datetime.now(timezone.utc).isoformat()
    classifier = None
    if not skip_classifier:
        X, y, meta = build_classifier_dataset(days=days)
        if len(X) < 40 or len(set(y)) < 2:
            print("not enough flagged telemetry to (re)train classifier; keeping existing")
        else:
            train_classifier(X, y, out_path=CLASSIFIER_PATH)
            classifier = load_classifier()
    else:
        classifier = load_classifier()

    calibration = learn_calibration_factors(days=days, classifier=classifier,
                                            min_factor_samples=min_factor_samples,
                                            min_family_samples=min_family_samples)
    if calibration is None:
        calibration = load_calibration()

    print(f"pipeline finished at {trained_at}")
    print(f"  classifier: {'trained/reloaded' if classifier else 'unavailable'}")
    if calibration:
        print(f"  calibration: global={calibration['global']}, "
              f"families={calibration.get('families', {})}")
    else:
        print("  calibration: none yet (blanket factor stays active)")
    return classifier, calibration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--skip-classifier", action="store_true")
    parser.add_argument("--min-factor-samples", type=int, default=5)
    parser.add_argument("--min-family-samples", type=int, default=2)
    args = parser.parse_args()
    try:
        run(days=args.days, skip_classifier=args.skip_classifier,
            min_factor_samples=args.min_factor_samples,
            min_family_samples=args.min_family_samples)
    except KeyboardInterrupt:
        print("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
