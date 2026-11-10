"""Train the supervised payload regressor on shipment ground truth.

Usage: python -m scripts.train_tonnage_regression [--days 365] [--skip-classifier]
Writes models/payload_regressor.txt. Re-run after re-ingesting shipments or
when more live telemetry accumulates.
"""

import argparse

from app.ml_tonnage import load_classifier
from app.tonnage_regression import build_regression_dataset, train_regressor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--skip-classifier", action="store_true",
                        help="don't ensure the loaded/empty classifier is present")
    args = parser.parse_args()

    if not args.skip_classifier and load_classifier() is None:
        print("no loaded/empty classifier found; train it first: "
              "python -m scripts.train_tonnage --skip-calibration")
        return 1

    X, y, meta = build_regression_dataset(days_back=args.days)
    if len(X) < 50:
        print(f"only {len(X)} windows; need >= 50. Keep collecting shipments/telemetry.")
        return 1
    result = train_regressor(X, y)
    print("done. mae=%.2ft r2=%.3f" % (result["mae"], result["r2"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
