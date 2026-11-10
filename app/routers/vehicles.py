"""Vehicle endpoints — fleet list, per-vehicle profile, tonnage, trips, dwells."""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user, require_admin
from app.db import connect
from app.driver_profiling import profile_vehicle
from app.ml_tonnage import calibration_factor_for, get_models
from app.tonnage import estimate_trip
from app.tonnage_regression import get_regressor, load_events
from app.trips import (dwell_events, dwell_summary, trip_comparison,
                       trips_for_day)

log = logging.getLogger(__name__)

router = APIRouter(tags=["vehicles"])


class KerbRequest(BaseModel):
    vehicle_no: str
    kerb_weight_kg: Optional[float] = None
    kerb_model: Optional[str] = None


# ---------- fleet list ----------


@router.get("/vehicles")
def list_vehicles(active_only: bool = True, user: dict = Depends(get_current_user)):
    query = (
        "SELECT DISTINCT intangle_id, vehicle_no, is_active, last_seen, "
        "payload_tonnes, total_mass_kg, tare_mass_kg, tonnage_note, "
        "kerb_weight_kg, kerb_model, gvw_kg, payload_capacity_kg, "
        "trailer_weight_kg, vehicle_type FROM vehicles"
    )
    if active_only:
        query += " WHERE is_active"
    query += " ORDER BY vehicle_no"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()


# ---------- per-vehicle endpoints ----------


@router.get("/vehicles/{vehicle_no}/profile")
def vehicle_profile(vehicle_no: str, days: int = 7, user: dict = Depends(get_current_user)):
    """Driver behaviour profile for a vehicle: existing profile if one exists,
    otherwise computed on-the-fly (read-only, never writes)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, driver_name, vehicle_no, profile_data, driving_patterns, "
                "confidence, trained_at FROM driver_profiles "
                "WHERE vehicle_no = %s ORDER BY updated_at DESC LIMIT 1",
                (vehicle_no,),
            )
            row = cur.fetchone()
    if row:
        return {
            "existing": True,
            "profile_id": row["id"],
            "driver_name": row["driver_name"],
            "vehicle_no": row["vehicle_no"],
            "patterns": row["driving_patterns"] or [],
            "profile_data": row["profile_data"] or {},
            "confidence": row["confidence"],
            "trained_at": row["trained_at"],
        }
    classifier, _ = get_models()
    try:
        result = profile_vehicle(vehicle_no, days=days, classifier=classifier)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"existing": False, **result, "trained_at": None, "profile_id": None}


@router.get("/vehicles/{vehicle_no}/load-events")
def vehicle_load_events(vehicle_no: str, day: date, user: dict = Depends(get_current_user)):
    """Load/unload events for a vehicle on a given day (IST).

    Uses the supervised payload regressor (trained on the client's shipment
    net weights) to estimate payload per driving window, then emits LOADED /
    UNLOADED events wherever the smoothed payload crosses the load threshold.
    Returns the events plus the payload-vs-time curve for the timeline chart.
    """
    return load_events(vehicle_no, str(day), model=get_regressor())


@router.get("/vehicles/{vehicle_no}/dwell-events")
def vehicle_dwell_events(vehicle_no: str, day: date = None, days: int = 1,
                         user: dict = Depends(get_current_user)):
    """Real LOADING / UNLOADING / STOP windows with timing, not markers.

    A truck doesn't load or unload in an instant -- it parks at the
    loader/receiver for ~an hour. Each dead-band gap between stored trips IS
    that window: this returns the window start/end, its duration, and the why
    (payload rose = loading, payload dropped = unloading, else a parked stop).
    `days` aggregates the count + average duration over N days.
    """
    if days and days > 1:
        return dwell_summary(vehicle_no, days)
    if day is None:
        raise HTTPException(status_code=422, detail="query param 'day' required when days<=1")
    return dwell_events(vehicle_no, str(day))


@router.get("/vehicles/{vehicle_no}/trips")
def vehicle_trips(vehicle_no: str, day: date, user: dict = Depends(get_current_user)):
    """Segmented trips for a vehicle on a given day (IST).

    Each trip is a contiguous driving run bounded by long stops; labelled
    loaded/empty/partial/unknown from the shipment-trained payload regressor.
    Computes + stores on the fly if the day hasn't been precomputed yet.
    """
    return trips_for_day(vehicle_no, str(day))


@router.get("/vehicles/{vehicle_no}/trip-comparison")
def vehicle_trip_comparison(vehicle_no: str, days: int = 14, user: dict = Depends(get_current_user)):
    """How this vehicle's OBD behaves EMPTY (A->B) vs LOADED (B->C).

    Aggregates stored trips by load label over the last N days: distance,
    fuel/km, km/L, engine load, rpm and speed for each bucket, plus the %
    change between loaded and empty legs. Also infers lading/unloading
    transitions anchored on dead-band gaps (gap_inferred_lading /
    gap_inferred_unloading) where the payload steps across a >10min gap.
    """
    return trip_comparison(vehicle_no, days)


# ---------- tonnage ----------


@router.get("/tonnage")
def tonnage_estimate(
    vehicle_no: str,
    days: int = 3,
    kerb_weight: Optional[float] = None,
    user: dict = Depends(get_current_user),
):
    """Physics-based payload estimate (virtual weighbridge) for a vehicle.

    Pulls telemetry for the last `days`, runs the energy-balance + accel +
    cruise estimators, returns total mass, payload tonnes and diagnostics.
    `kerb_weight` overrides the vehicle's stored kerb_weight_kg (set via
    POST /vehicles/kerb); otherwise the stored value / model / family default.
    """
    sql = """
        SELECT time, speed, rpm, gear, fuel_rate, engine_load, accel_pedal, vehicle_load_cond
        FROM telemetry t
        JOIN vehicles v ON v.intangle_id = t.vehicle_id
        WHERE v.vehicle_no = %s AND t.time > now() - make_interval(days => %s)
          AND t.speed > 2 AND t.rpm > 0 AND t.fuel_rate > 0
        ORDER BY t.time
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kerb_weight_kg, kerb_model, gvw_kg, payload_capacity_kg, "
                "trailer_weight_kg, vehicle_type FROM vehicles WHERE vehicle_no = %s",
                (vehicle_no,),
            )
            meta = cur.fetchone()
            cur.execute(sql, (vehicle_no, days))
            rows = cur.fetchall()
    stored_kerb = (meta or {}).get("kerb_weight_kg")
    kerb_model = (meta or {}).get("kerb_model")
    gvw_kg = (meta or {}).get("gvw_kg")
    payload_capacity_kg = (meta or {}).get("payload_capacity_kg")
    trailer_weight_kg = (meta or {}).get("trailer_weight_kg")
    vehicle_type = (meta or {}).get("vehicle_type")
    classifier, calibration = get_models()
    est = estimate_trip(
        [dict(r) for r in rows],
        vehicle_no=vehicle_no,
        kerb_weight=kerb_weight if kerb_weight is not None else stored_kerb,
        kerb_model=kerb_model,
        family="blazo_x48t",
        gvw_kg=gvw_kg,
        payload_capacity_kg=payload_capacity_kg,
        trailer_weight_kg=trailer_weight_kg,
        vehicle_type=vehicle_type,
        calibration_factor=calibration_factor_for(vehicle_type, calibration),
        classifier=classifier,
    )
    return {
        "vehicle_no": est.vehicle_no,
        "kerb_weight": est.kerb_weight,
        "kerb_model": kerb_model,
        "gvw_kg": est.gvw_kg,
        "payload_capacity_kg": est.payload_capacity_kg,
        "trailer_weight_kg": trailer_weight_kg,
        "vehicle_type": vehicle_type,
        "tare_source": est.tare_source,
        "total_mass_kg": est.total_mass_kg,
        "tare_mass_kg": est.tare_mass_kg,
        "payload_tonnes": est.payload_tonnes,
        "windows_used": est.windows_used,
        "windows_total": est.windows_total,
        "method": est.method,
        "note": est.note,
        "behaviour_score": est.behaviour_score,
        "behaviour_mass_kg": est.behaviour_mass_kg,
        "behaviour_calib": est.behaviour_calib,
        "raw_estimates": est.raw_masses,
        "gear_ratios": est.gear_ratios,
        "samples": len(rows),
    }


# ---------- kerb weight management ----------


@router.post("/vehicles/kerb")
def set_kerb(req: KerbRequest, user: dict = Depends(require_admin)):
    """Set a vehicle's kerb (tare) weight / model. kerb_weight_kg overrides the
    model lookup in the solver. Leave both None to clear the override."""
    if req.kerb_weight_kg is not None and req.kerb_weight_kg <= 0:
        raise HTTPException(status_code=400, detail="kerb_weight_kg must be positive")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicles SET kerb_weight_kg = %s, kerb_model = %s "
                "WHERE vehicle_no = %s",
                (req.kerb_weight_kg, req.kerb_model, req.vehicle_no),
            )
            updated = cur.rowcount
        conn.commit()
    if not updated:
        raise HTTPException(status_code=404, detail=f"no vehicle {req.vehicle_no!r}")
    return {"updated": updated, "vehicle_no": req.vehicle_no,
            "kerb_weight_kg": req.kerb_weight_kg, "kerb_model": req.kerb_model}


@router.delete("/vehicles/kerb")
def clear_kerb(vehicle_no: str, user: dict = Depends(require_admin)):
    """Drop the per-vehicle kerb override (falls back to model / family)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicles SET kerb_weight_kg = NULL, kerb_model = NULL "
                "WHERE vehicle_no = %s",
                (vehicle_no,),
            )
            updated = cur.rowcount
        conn.commit()
    if not updated:
        raise HTTPException(status_code=404, detail=f"no vehicle {vehicle_no!r}")
    return {"updated": updated, "vehicle_no": vehicle_no}
