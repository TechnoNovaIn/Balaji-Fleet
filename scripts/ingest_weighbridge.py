"""Ingest the 1-year weighbridge ground-truth workbook into `shipments`.

Source: the client's Excel (one sheet per vehicle) with columns
shipmentId, vehicle, ShipmentType, NtWt, ShipmentDate, PartyName, VehicleType.
ShipmentDate is an Excel serial (days since 1899-12-30); NtWt is tonnes.

This script:
  1. reads every sheet, concatenates, converts dates, normalizes plates,
  2. drops junk weights (>100 t = kg-typed entries, ==0),
  3. writes data/shipments.csv in the standard ingest schema, and
  4. ingests it via scripts.ingest_shipments (idempotent on shipment_id).

Re-run safely after a refresh.
"""

import argparse
import csv
import datetime
import os

import pandas as pd

from scripts.ingest_shipments import ingest_shipments

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "shipments.csv")

WEIGHT_SERIAL_EPOCH = datetime.date(1899, 12, 30)
MAX_SANE_WEIGHT_T = 100.0


def xls_serial_to_date(v):
    if v is None or pd.isna(v):
        return None
    return (WEIGHT_SERIAL_EPOCH + datetime.timedelta(days=int(v))).isoformat()


def convert_workbook(path):
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        df["sheet"] = sheet
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    df["date"] = df["ShipmentDate"].apply(xls_serial_to_date)
    df["vehicle_clean"] = df["vehicle"].astype(str).str.replace(" ", "", regex=False).str.upper()

    out = df[
        (df["NtWt"] > 0)
        & (df["NtWt"] <= MAX_SANE_WEIGHT_T)
        & (df["date"].notna())
    ].copy()
    dropped = len(df) - len(out)

    rows = []
    for _, r in out.iterrows():
        try:
            wt = float(r["NtWt"])
        except (TypeError, ValueError):
            continue
        rows.append({
            "shipment_id": int(r["shipmentId"]),
            "vehicle": r["vehicle_clean"],
            "shipment_type": r.get("ShipmentType"),
            "net_weight_t": wt,
            "shipment_date": r["date"],
            "party": r.get("PartyName"),
            "vehicle_type": r.get("VehicleType"),
        })
    return rows, dropped


def main():
    parser = argparse.ArgumentParser(description="Convert client 1-year weighbridge Excel to shipments.csv + ingest")
    parser.add_argument("--path", required=True, help="Path to the .xlsx (client weighbridge workbook)")
    parser.add_argument("--no-ingest", action="store_true", help="Only write the CSV, skip DB ingest")
    args = parser.parse_args()

    rows, dropped = convert_workbook(args.path)
    print(f"workbook: {len(rows)} usable rows ({dropped} dropped as junk/zero-weight)")

    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "shipment_id", "vehicle", "shipment_type", "net_weight_t",
            "shipment_date", "party", "vehicle_type",
        ])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {CSV_PATH}")

    if not args.no_ingest:
        ingest_shipments(CSV_PATH)


if __name__ == "__main__":
    main()
