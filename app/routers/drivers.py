"""Driver profiling endpoints — list, create, update, delete, train."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user, require_admin
from app.db import connect
from app.driver_profiling import profile_vehicle
from app.ml_tonnage import get_models

log = logging.getLogger(__name__)

router = APIRouter(prefix="/driver-profiles", tags=["drivers"])


@router.get("")
def list_driver_profiles(
    driver_name: Optional[str] = None,
    vehicle_no: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """List driver profiles, optionally filtered by driver name or vehicle."""
    query = (
        "SELECT id, driver_name, vehicle_no, profile_data, driving_patterns, "
        "route_patterns, trained_at, confidence, created_at FROM driver_profiles WHERE 1=1"
    )
    params = []
    if driver_name:
        query += " AND driver_name ILIKE %s"
        params.append(f"%{driver_name}%")
    if vehicle_no:
        query += " AND vehicle_no ILIKE %s"
        params.append(f"%{vehicle_no}%")
    query += " ORDER BY updated_at DESC"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


@router.post("")
def create_driver_profile(req: dict, user: dict = Depends(require_admin)):
    """Create a new driver profile."""
    driver_name = (req.get("driver_name") or "").strip()
    vehicle_no = (req.get("vehicle_no") or "").strip().upper()
    if not driver_name:
        raise HTTPException(status_code=400, detail="driver_name is required")
    if not vehicle_no:
        raise HTTPException(status_code=400, detail="vehicle_no is required")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO driver_profiles (driver_name, vehicle_no, profile_data, driving_patterns, route_patterns) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    driver_name,
                    vehicle_no,
                    json.dumps(req.get("profile_data", {})),
                    json.dumps(req.get("driving_patterns", [])),
                    json.dumps(req.get("route_patterns", [])),
                ),
            )
            profile_id = cur.fetchone()["id"]
        conn.commit()
    return {"id": profile_id, "driver_name": driver_name, "vehicle_no": vehicle_no}


@router.put("/{profile_id}")
def update_driver_profile(profile_id: int, req: dict, user: dict = Depends(require_admin)):
    """Update a driver profile."""
    fields = []
    params = []
    for key in (
        "driver_name",
        "vehicle_no",
        "profile_data",
        "driving_patterns",
        "route_patterns",
        "trained_at",
        "confidence",
    ):
        if key in req:
            val = req[key]
            if isinstance(val, (dict, list)):
                val = json.dumps(val)
            fields.append(f"{key} = %s")
            params.append(val)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    fields.append("updated_at = now()")
    params.append(profile_id)
    query = f"UPDATE driver_profiles SET {', '.join(fields)} WHERE id = %s"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            updated = cur.rowcount
        conn.commit()
    if not updated:
        raise HTTPException(status_code=404, detail=f"no profile with id {profile_id}")
    return {"updated": updated, "id": profile_id}


@router.delete("/{profile_id}")
def delete_driver_profile(profile_id: int, user: dict = Depends(require_admin)):
    """Delete a driver profile."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM driver_profiles WHERE id = %s", (profile_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no profile with id {profile_id}")
    return {"deleted": deleted, "id": profile_id}


@router.post("/{profile_id}/train")
def train_driver_profile(profile_id: int, days: int = 7, user: dict = Depends(require_admin)):
    """Train a driver profile from telemetry: behaviour features + clustering."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM driver_profiles WHERE id = %s", (profile_id,))
            profile = cur.fetchone()
            if not profile:
                raise HTTPException(status_code=404, detail=f"no profile with id {profile_id}")
            vehicle_no = profile.get("vehicle_no")
    if not vehicle_no:
        raise HTTPException(status_code=400, detail="profile has no vehicle_no to analyze")

    classifier, _ = get_models()
    try:
        result = profile_vehicle(vehicle_no, days=days, classifier=classifier)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE driver_profiles SET trained_at = now(), confidence = %s, "
                "driving_patterns = %s, profile_data = %s, updated_at = now() "
                "WHERE id = %s",
                (
                    result["confidence"],
                    json.dumps(result["patterns"]),
                    json.dumps(result["profile_data"]),
                    profile_id,
                ),
            )
        conn.commit()
    return {"trained": True, "profile_id": profile_id, **result}
