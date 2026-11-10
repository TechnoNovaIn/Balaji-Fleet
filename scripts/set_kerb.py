"""Load a per-vehicle kerb (tare) weight table into the vehicles DB.

Usage:
    python -m scripts.set_kerb kerb.csv [--dry-run]

CSV columns: vehicle_no,model[,kerb_weight_kg]
  - `vehicle_no`     required. Plate or VIN alias.
  - `model`          optional. BharatBenz model code (1217R, 1617R, 4028T, ...),
                     resolved to a kerb weight via app.tonnage.KERB_MODELS.
  - `kerb_weight_kg` optional. Manufacturer kerb weight in kg; takes priority
                     over a model lookup when both are present.

At least one of `model` / `kerb_weight_kg` must be given per row. Unknown
models (not in KERB_MODELS) are skipped and reported unless the row also
carries an explicit weight. Idempotent; safe to re-run.

Header line is required and column order/extra columns are ignored.
"""

import argparse
import csv
import sys

from app.db import connect
from app.tonnage import KERB_MODELS


def run(path, dry_run=False):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            r = {k.strip().lower(): v.strip() for k, v in r.items() if k}
            vehicle_no = r.get("vehicle_no")
            if not vehicle_no:
                continue
            model = r.get("model") or ""
            try:
                weight = float(r["kerb_weight_kg"]) if r.get("kerb_weight_kg") else None
            except ValueError:
                print(f"  skip {vehicle_no}: bad kerb_weight_kg={r.get('kerb_weight_kg')!r}")
                continue
            if weight is None and model:
                weight = KERB_MODELS.get(model)
            if weight is None:
                print(f"  skip {vehicle_no}: no weight and model {model!r} not in KERB_MODELS")
                continue
            rows.append((vehicle_no, weight, model))

    print(f"loaded {len(rows)} rows from {path}")
    if not rows:
        return 0

    if dry_run:
        for vn, w, m in rows:
            print(f"  would set {vn}: kerb_weight_kg={w} kerb_model={m or ''}")
        return len(rows)

    updated = 0
    with connect() as conn:
        for vn, w, m in rows:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vehicles SET kerb_weight_kg = %s, kerb_model = %s "
                    "WHERE vehicle_no = %s",
                    (w, m or None, vn),
                )
                updated += cur.rowcount
        conn.commit()
    print(f"updated {updated}/{len(rows)} vehicles")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[1].split("\n")[0].strip())
    parser.add_argument("csv", help="CSV with vehicle_no,model,kerb_weight_kg")
    parser.add_argument("--dry-run", action="store_true", help="print what would change")
    args = parser.parse_args()
    try:
        run(args.csv, dry_run=args.dry_run)
    except FileNotFoundError:
        print(f"no such file: {args.csv}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
