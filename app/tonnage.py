"""Physics-based payload ("virtual weighbridge") solver.

Longitudinal dynamics for a truck on the road:

    F_tractive = m*a + m*g*Crr + 0.5*rho*CdA*v^2 + m*g*sin(theta)

Engine brake power from OBD fuel rate (L/h):

    P_engine = FR/3600 * rho_fuel * LHV * eta_engine

Tractive force at wheels: F = P_engine * eta_drivetrain / v.

Energy balance over a window [t0, t1] (flat-road assumption, theta ~ 0):

    eta_dt * int(P_engine) dt = 0.5*m*(v1^2 - v0^2) + m*g*Crr*int(v) dt
                                + 0.5*rho*CdA*int(v^3) dt

Solving for total vehicle mass m:

    m = [eta_dt*int(P_engine)dt - 0.5*rho*CdA*int(v^3)dt]
        / [0.5*(v1^2 - v0^2) + g*Crr*int(v)dt]

    payload_tonnes = (m - kerb_weight) / 1000

Instantaneous estimator (well-conditioned only during acceleration, a >> 0):

    m = [eta_dt*P_engine/v - 0.5*rho*CdA*v^2] / (a + g*Crr)

Gear ratios: at fixed gear G, rpm/v is a drivetrain constant (kinematic),
learned from steady-state windows per vehicle; used to validate speed/rpm
consistency before a window is trusted for energy balance.
"""

import statistics
from dataclasses import dataclass, field

G = 9.81                     # m/s^2
RHO_AIR = 1.225              # kg/m^3
FUEL_DENSITY = 0.845         # kg/L diesel
FUEL_LHV = 42.6e6            # J/kg
ENGINE_EFF = 0.40            # brake thermal efficiency at operating point
DRIVETRAIN_EFF = 0.88        # gearbox + diff
CRR_DEFAULT = 0.009          # rolling resistance coefficient (asphalt)
CDA_DEFAULT = 6.5            # m^2, heavy truck Cd~0.6 x A~10.8

# Typical tare (kerb) weights per truck family, kg. Heuristic defaults;
# calibrate per family with weighbridge labels.
KERB_WEIGHT_KG = {
    "default": 7500,          # 4x2 tractor alone
    "blazo_x48t": 6500,       # 6x4 rigid/tractor tare
    "truck_16ft": 5200,       # 16 ft rigid body
    "truck_20ft": 5600,
    "truck_24ft": 6100,
    "trailer": 6500,          # tractor + empty trailer
}

# Per-model kerb weights, kg, supplied by the client (BharatBenz model code
# -> manufacturer kerb weight). Populate from the fleet's model->kerb table;
# `scripts/set_kerb.py` writes the resolved value to vehicles.kerb_weight_kg.
KERB_MODELS = {
    # e.g. "1217R": 5200, "1617R": 5600, "4028T": 4500, ...
}

JUNK_RPM0_SPEED_MIN = 5       # rpm=0 while rolling => stale/hold frame

# Physics mass calibration factor. The energy-balance solver systematically
# under-estimates total mass because OBD fuel rate under-reports real fuel
# consumption (engine heat losses, measurement lag, etc.). Calibrated from
# the client's spec sheet: known unladen weight / solver empty estimate.
# Re-fit when weighbridge or axle-load labels arrive.
TONNAGE_CALIBRATION_FACTOR = 2.0

# Behavioural ("how the driver runs it") load estimator.
# Loaded trucks sit at higher rpm, higher engine_load% and higher fuel rate
# than the SAME truck running empty at the same speed. The estimator builds a
# per-vehicle EMPTY baseline per speed band (rpm / engine_load% / fuel_rate),
# then scores each driving row by its throttle-controlled deviation from it.
BEHAVIOUR_SPEED_BANDS = [(10, 20), (20, 30), (30, 40), (40, 50), (50, 70)]
BEHAVIOUR_THROTTLE_MAX = 40.0  # accel% at/below this = steady throttle, full trust
BEHAVIOUR_MAD_FLOORS = {"mad_load": 3.0, "mad_fuel": 1.0, "mad_rpm": 80.0}
BEHAVIOUR_MIN_PAIRS = 15       # min (score, accel-mass) pairs to calibrate slope
BEHAVIOUR_MIN_SCORES = 20      # min scored rows to trust the behavioural estimate
# Fleet fallback: tonnes of payload per unit behaviour score. Calibrated from
# the one weighbridge-labelled truck (KA06AD1203: 4.6t empty -> 9.1t loaded
# over score 0 -> 0.72). Replace with per-family values once weighbridge or
# axle-load labels exist.
BEHAVIOUR_TONNES_PER_SCORE = 7.0


def engine_power_w(fuel_rate_lh):
    """Fuel rate (L/h) -> engine brake power (W)."""
    return fuel_rate_lh / 3600.0 * FUEL_DENSITY * FUEL_LHV * ENGINE_EFF


def required_fuel_lh(power_w):
    """Engine brake power (W) -> fuel rate (L/h), inverse of engine_power_w."""
    return power_w * 3600.0 / (FUEL_DENSITY * FUEL_LHV * ENGINE_EFF)


def learn_gear_ratios(rows, min_per_gear=5):
    """Return {gear: median rpm/speed} from steady-state windows.

    At fixed gear G the drivetrain is kinematic: rpm/v = 60*R_G/(2*pi*r_wheel),
    a per-gear constant. Only near-steady samples (small |dv/dt|) are used so
    shift transients and 5s sampling lag do not smear the ratio.
    """
    ratios = {}
    for r in rows:
        speed = r.get("speed")
        rpm = r.get("rpm")
        gear = r.get("gear")
        if not speed or speed < 10 or not rpm or rpm < 500 or not gear:
            continue
        ratios.setdefault(gear, []).append(rpm / speed)
    learned = {}
    for gear, vals in ratios.items():
        if len(vals) >= min_per_gear:
            learned[gear] = statistics.median(vals)
    return learned


def _clean(rows):
    """Drop junk/hold frames (engine off but rolling) and stationary rows."""
    clean = []
    for r in rows:
        speed = r.get("speed")
        rpm = r.get("rpm")
        fuel = r.get("fuel_rate")
        if speed is None or rpm is None or fuel is None:
            continue
        if speed is not None and speed > JUNK_RPM0_SPEED_MIN and rpm == 0:
            continue  # kinematically impossible -> stale echo frame
        if speed < 2:
            continue  # not useful for dynamics
        clean.append(r)
    return clean


def _power_from_rows(rows):
    return [engine_power_w(r["fuel_rate"]) for r in rows]


def mass_from_energy_balance(rows, crr=CRR_DEFAULT, cda=CDA_DEFAULT):
    """Total mass (kg) from one clean acceleration window via energy balance."""
    n = len(rows)
    if n < 3:
        return None
    v0 = rows[0]["speed"] / 3.6
    v1 = rows[-1]["speed"] / 3.6
    dke = 0.5 * (v1 * v1 - v0 * v0)
    if abs(dke) < 0.5:
        return None  # near-constant speed: degenerate window
    dt = 5.0  # sample period, s
    e_engine = sum(_power_from_rows(rows)) * dt * DRIVETRAIN_EFF
    e_aero = 0.5 * RHO_AIR * cda * sum((r["speed"] / 3.6) ** 3 for r in rows) * dt
    work_roll = G * crr * sum(r["speed"] / 3.6 for r in rows) * dt
    denom = dke + work_roll
    if denom <= 0:
        return None
    return (e_engine - e_aero) / denom


def mass_from_acceleration(rows, crr=CRR_DEFAULT, cda=CDA_DEFAULT):
    """Instantaneous mass estimates (kg) during strong acceleration.

    Well-conditioned only when a >> g*Crr (0.09 m/s^2), so grade and
    parameter errors are diluted by the large denominator.
    """
    out = []
    dt = 5.0
    for i in range(1, len(rows) - 1):
        v = rows[i]["speed"] / 3.6
        if v < 2.0:
            continue
        a = (rows[i + 1]["speed"] - rows[i - 1]["speed"]) / 3.6 / (2 * dt)
        if a < 0.35:
            continue
        p_wheels = engine_power_w(rows[i]["fuel_rate"]) * DRIVETRAIN_EFF
        f_tractive = p_wheels / v
        f_aero = 0.5 * RHO_AIR * cda * v * v
        denom = a + G * crr
        m = (f_tractive - f_aero) / denom
        if 2000 < m < 120000:
            out.append(m)
    return out


def mass_from_cruise(rows, crr=CRR_DEFAULT, cda=CDA_DEFAULT):
    """Mass (kg) from near-steady-speed windows (a ~ 0).

    P_wheels = m*g*Crr*v + 0.5*rho*CdA*v^3  =>  m = (P - aero)/ (g*Crr*v).
    Conditioned when rolling resistance dominates aero (low speed) and the
    speed is genuinely steady.
    """
    out = []
    dt = 5.0
    for i in range(2, len(rows) - 2):
        v = rows[i]["speed"] / 3.6
        if v < 5.0:
            continue
        a = (rows[i + 1]["speed"] - rows[i - 1]["speed"]) / 3.6 / (2 * dt)
        if abs(a) > 0.06:
            continue  # not steady
        p_wheels = engine_power_w(rows[i]["fuel_rate"]) * DRIVETRAIN_EFF
        f_aero = 0.5 * RHO_AIR * cda * v * v
        f_roll = G * crr * v
        m = (p_wheels / v - f_aero) / f_roll
        if 2000 < m < 120000:
            out.append(m)
    return out


def _robust_estimate(masses, max_mass=150000):
    """IQR-trimmed median: grade contamination pushes tails both ways, so the
    central half of estimates is the load-observable signal."""
    if not masses:
        return None
    m = sorted(float(x) for x in masses)
    n = len(m)
    if n == 1:
        return m[0] if m[0] <= max_mass else None
    if n == 2:
        med = (m[0] + m[1]) / 2
        return med if abs(m[0] - m[1]) / med < 0.5 and med <= max_mass else None
    q1 = m[max(0, n // 4 - 1)]
    q3 = m[min(n - 1, 3 * n // 4)]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    keep = [x for x in m if lo <= x <= hi and x <= max_mass]
    if not keep:
        return None
    k = len(keep)
    return statistics.median(keep[k // 4: k - k // 4]) if k >= 4 else statistics.median(keep)


@dataclass
class TripEstimate:
    vehicle_no: str
    kerb_weight: float
    total_mass_kg: float = None
    tare_mass_kg: float = None
    payload_tonnes: float = None
    tare_source: str = ""
    windows_used: int = 0
    windows_total: int = 0
    method: str = "energy"
    raw_masses: list = field(default_factory=list)
    gear_ratios: dict = field(default_factory=dict)
    behaviour_score: float = None
    behaviour_mass_kg: float = None
    behaviour_calib: str = None
    gvw_kg: float = None
    payload_capacity_kg: float = None
    note: str = ""


def _windows_from_rows(clean, max_speed_jump=40, min_len=4):
    """Split into contiguous driving runs broken by large speed gaps/shifts."""
    windows = []
    cur = [clean[0]]
    for r in clean[1:]:
        if abs(r["speed"] - cur[-1]["speed"]) > max_speed_jump:
            windows.append(cur)
            cur = [r]
        else:
            cur.append(r)
    windows.append(cur)
    return [w for w in windows if len(w) >= min_len]


def estimate_trip(rows, vehicle_no="", kerb_weight=None, family="default", kerb_model=None,
                  gvw_kg=None, payload_capacity_kg=None, trailer_weight_kg=None,
                  vehicle_type=None, calibration_factor=None, classifier=None,
                  empty_prob_threshold=0.25):
    """Estimate payload for one trip (list of telemetry dicts).

    Strategy: clean junk frames, then solve total vehicle mass three ways --
    energy balance over accelerating runs, instantaneous force balance during
    hard acceleration, and rolling-resistance balance at steady cruise. Each
    method rejects the windows where it is ill-conditioned. The three estimate
    sets are pooled and a robust median is taken; grade (unknown without GPS)
    is the main residual source, so windows where acceleration is large are
    weighted above all others.

    Payload (exact tonnage) = total_mass - tare, where tare is calibrated
    from the vehicle's OWN empty-condition driving rather than a guessed kerb
    weight: windows flagged `vehicle_load_cond='01'` (empty), else windows the
    loaded/empty ML classifier scores as empty, else the low tail of its own
    mass distribution (empty legs). This cancels grade/kerb bias because the
    reference and the load run share the same unknowns.

    `calibration_factor` overrides the blanket TONNAGE_CALIBRATION_FACTOR with a
    per-family value learned from RTO kerb anchors (see app/ml_tonnage.py).
    `classifier` is a trained LightGBM Booster scoring P(loaded) per window,
    used to find empty windows for tare when the truck emits no load flag.

    Kerb weight (used only when no self-calibrated tare exists) resolves:
    explicit kerb_weight > KERB_MODELS[kerb_model] > KERB_WEIGHT_KG[family].
    """
    cal = calibration_factor or TONNAGE_CALIBRATION_FACTOR
    kerb = (
        kerb_weight
        or KERB_MODELS.get(kerb_model)
        or KERB_WEIGHT_KG.get(family, KERB_WEIGHT_KG["default"])
    )
    clean = _clean(rows)
    if len(clean) < 6:
        return TripEstimate(vehicle_no, kerb, gvw_kg=gvw_kg,
                            payload_capacity_kg=payload_capacity_kg,
                            note="not enough clean telemetry")

    gear_ratios = learn_gear_ratios(clean)
    masses, energy, accel, cruise, windows = _pooled_masses(clean, calibration_factor=cal)
    if not (energy or accel or cruise):
        return TripEstimate(vehicle_no, kerb, gvw_kg=gvw_kg,
                            payload_capacity_kg=payload_capacity_kg,
                            windows_total=len(windows),
                            gear_ratios=gear_ratios,
                            note="no well-conditioned windows")

    raw_total = _robust_estimate(masses)
    if raw_total is None or len(masses) < 3:
        return TripEstimate(vehicle_no, kerb, gvw_kg=gvw_kg,
                            payload_capacity_kg=payload_capacity_kg,
                            windows_total=len(windows),
                            gear_ratios=gear_ratios, raw_masses=[round(x) for x in masses],
                            note="too little clean data to trust an estimate")

    self_tare = _self_calibrated_tare(clean, calibration_factor=cal)
    tare_source = "self-calibrated"
    if self_tare is None and classifier is not None:
        self_tare = _classifier_tare(clean, classifier, total_mass=raw_total,
                                     calibration_factor=cal,
                                     threshold=empty_prob_threshold)
        tare_source = "ml-empty" if self_tare else "self-calibrated"
    if self_tare is None:
        tare = _low_tail_tare(masses, raw_total, kerb)
        tare_source = "low-tail" if tare else "kerb"
    else:
        tare = self_tare
    tare_used = tare if tare else kerb
    if trailer_weight_kg and trailer_weight_kg > 0:
        tare_used = tare_used + trailer_weight_kg

    behaviour_mass, behaviour_score, behaviour_calib = _behaviour_mass(
        clean,
        _build_behavior_baseline(_empty_reference_rows(clean)),
        self_tare if self_tare is not None else kerb,   # true empty mass, not low-tail
    )
    pooled = list(masses)
    method = "energy+accel+cruise"
    if (behaviour_mass is not None and tare_used
            and 0.6 * raw_total <= behaviour_mass <= 1.6 * raw_total):
        pooled.append(behaviour_mass)
        method += "+behaviour"

    total_mass = _robust_estimate(pooled)
    payload = max(0.0, (total_mass - tare_used) / 1000.0)
    return TripEstimate(
        vehicle_no=vehicle_no,
        kerb_weight=kerb,
        gvw_kg=gvw_kg,
        payload_capacity_kg=payload_capacity_kg,
        total_mass_kg=round(total_mass),
        tare_mass_kg=round(tare_used),
        payload_tonnes=round(payload, 2),
        tare_source=tare_source,
        windows_used=len(accel) + len(energy) + len(cruise),
        windows_total=len(windows),
        method=method,
        raw_masses=[round(x) for x in pooled],
        gear_ratios=gear_ratios,
        behaviour_score=round(behaviour_score, 2) if behaviour_score is not None else None,
        behaviour_mass_kg=round(behaviour_mass) if behaviour_mass is not None else None,
        behaviour_calib=behaviour_calib,
    )


def _classifier_tare(clean, classifier, total_mass=None, calibration_factor=TONNAGE_CALIBRATION_FACTOR,
                     threshold=0.25, min_rows=40):
    """Tare from rows in windows the ML classifier scores as empty.

    The classifier finds low P(loaded) sliding windows; those rows are solved
    with the SAME pooled estimator mix as the load estimate (energy + accel +
    cruise + robust median), matching _self_calibrated_tare. Guards: enough
    empty rows, a physical floor, and tare clearly below the load total so a
    bad classifier pass can't invert the payload. Returns None on any failure;
    the caller then uses the low-tail / kerb fallback.
    """
    from app.ml_tonnage import window_features, sliding_windows, FEATURES
    import numpy as np
    windows = sliding_windows(clean)
    if len(windows) < 4:
        return None
    X = np.asarray([[window_features(w).get(k) for k in FEATURES] for w in windows], dtype=float)
    probs = classifier.predict(X)
    empty_rows = [r for w, p in zip(windows, probs) if p < threshold for r in w]
    if len(empty_rows) < min_rows:
        return None
    masses, *_ = _pooled_masses(empty_rows, calibration_factor=calibration_factor)
    if len(masses) < 4:
        return None
    est = _robust_estimate(masses)
    if not est or est <= 4000:
        return None
    if total_mass and est >= total_mass * 0.92:
        return None  # tare must be below the load estimate
    return est


def _pooled_masses(clean, calibration_factor=TONNAGE_CALIBRATION_FACTOR):
    """Run all three estimators over windows; return pooled masses + per-method lists.
    All masses are scaled by the calibration factor to correct the systematic
    under-estimation from OBD fuel-rate bias. Pass calibration_factor=1.0 to get
    raw (unscaled) masses, e.g. when learning the factor from an anchor.
    """
    windows = _windows_from_rows(clean)
    energy = []
    accel = []
    cruise = []
    for w in windows:
        m = mass_from_energy_balance(w)
        if m:
            energy.append(m * calibration_factor)
        accel.extend(m * calibration_factor for m in mass_from_acceleration(w))
        cruise.extend(m * calibration_factor for m in mass_from_cruise(w))
    if accel:
        masses = accel * 3 + energy + cruise
    elif energy:
        masses = energy
    else:
        masses = cruise
    return masses, energy, accel, cruise, windows


def _self_calibrated_tare(rows, min_mass=4000, calibration_factor=TONNAGE_CALIBRATION_FACTOR):
    """Tare weight from the vehicle's own empty-condition driving (kg).

    Uses empty-flagged windows ('01') filtered to real driving, solved with the
    SAME pooled estimator mix as the load estimate, so grade/kerb bias cancels
    (reference and load run share the same unknowns). Returns None when no
    trustworthy empty reference exists (caller falls back to family kerb).
    """
    flag_empty = [
        r for r in rows
        if r.get("vehicle_load_cond") == "01"
        and r.get("speed") and r["speed"] > 10
        and r.get("rpm") and r["rpm"] > 400
    ]
    if len(flag_empty) < 8:
        return None
    masses, *_ = _pooled_masses(flag_empty, calibration_factor=calibration_factor)
    if not masses:
        return None
    est = _robust_estimate(masses)
    if est and est > min_mass:
        return est
    return None


def _low_tail_tare(masses, total_mass, kerb, floor=5000):
    """Fallback tare from the empty-leg floor of the mass distribution.

    Used when no empty-condition flag is available. The low tail of a truck's
    mass estimates corresponds to its lightest (empty) runs; using the 15th
    percentile of the robust central band avoids the near-zero downhill tail.
    Rejected if below a physical floor or not clearly below the load estimate.
    """
    m = sorted(float(x) for x in masses)
    if len(m) < 10:
        return None
    med = statistics.median(m)
    tight = [x for x in m if abs(x - med) / med < 0.35]
    if len(tight) < 5:
        return None
    q = sorted(tight)[max(0, len(tight) // 5 - 1)]
    if q < floor or q >= total_mass * 0.9 or q > kerb * 1.8:
        return None
    return q


def _speed_band(speed):
    for lo, hi in BEHAVIOUR_SPEED_BANDS:
        if lo <= speed < hi:
            return (lo, hi)
    return None


def _mad(vals):
    med = statistics.median(vals)
    return statistics.median([abs(x - med) for x in vals])


def _empty_reference_rows(clean):
    """Rows showing the truck running LIGHT: either the ECU's own '01'
    (empty) flag, or (no flag) self-calibrated steady low-throttle/low-load
    rows. These become the speed-band EMPTY baseline the load is scored against.
    """
    flagged = [
        r for r in clean
        if r.get("vehicle_load_cond") == "01"
        and r.get("speed", 0) > 10 and r.get("rpm", 0) > 500 and r.get("gear", 0) > 0
        and r.get("engine_load") is not None and r.get("fuel_rate")
    ]
    if len(flagged) >= 20:
        return flagged
    loads = [r["engine_load"] for r in clean if r.get("engine_load") is not None]
    if not loads:
        return []
    med_load = statistics.median(loads)
    accels = [r["accel_pedal"] for r in clean if r.get("accel_pedal") is not None]
    med_accel = statistics.median(accels) if accels else 60.0
    out = []
    for i, r in enumerate(clean):
        if r.get("speed", 0) <= 10 or r.get("rpm", 0) <= 500 or r.get("gear", 0) == 0:
            continue
        if r.get("engine_load") is None or r.get("fuel_rate") is None:
            continue
        if i < 1 or i >= len(clean) - 1:
            continue
        dv = abs(clean[i + 1]["speed"] - clean[i - 1]["speed"]) / 36.0  # m/s^2 (10 s)
        if dv > 0.15:
            continue
        accel = r.get("accel_pedal")
        if accel is None:
            accel = med_accel
        if accel <= BEHAVIOUR_THROTTLE_MAX and r["engine_load"] <= med_load * 0.9:
            out.append(r)
    return out


def _build_behavior_baseline(ref_rows, min_per_band=5):
    """{speed_band: {'n','load','fuel','rpm','mad_load','mad_fuel','mad_rpm'}}."""
    bands = {}
    for r in ref_rows:
        b = _speed_band(r.get("speed"))
        if not b or r.get("engine_load") is None or r.get("fuel_rate") is None or not r.get("rpm"):
            continue
        bands.setdefault(b, []).append(r)
    base = {}
    for b, rows in bands.items():
        if len(rows) < min_per_band:
            continue
        base[b] = {
            "n": len(rows),
            "load": statistics.median(r["engine_load"] for r in rows),
            "fuel": statistics.median(r["fuel_rate"] for r in rows),
            "rpm": statistics.median(r["rpm"] for r in rows),
            "mad_load": max(_mad([r["engine_load"] for r in rows]), BEHAVIOUR_MAD_FLOORS["mad_load"]),
            "mad_fuel": max(_mad([r["fuel_rate"] for r in rows]), BEHAVIOUR_MAD_FLOORS["mad_fuel"]),
            "mad_rpm": max(_mad([r["rpm"] for r in rows]), BEHAVIOUR_MAD_FLOORS["mad_rpm"]),
        }
    return base


def behavior_score(row, base):
    """Throttle-controlled deviation of (engine_load%, fuel_rate, rpm) above
    the vehicle's own empty baseline at this speed band, in baseline-MAD units.
    Positive = driving harder than empty (load). Rows only count while the
    driver is actually on the power (gear engaged, revving)."""
    b = _speed_band(row.get("speed"))
    if not b or b not in base:
        return None
    if row.get("speed", 0) <= 10 or row.get("rpm", 0) <= 500 or row.get("gear", 0) == 0:
        return None
    if row.get("engine_load") is None or not row.get("fuel_rate"):
        return None
    accel = row.get("accel_pedal")
    trust = 1.0 if accel is None or accel <= BEHAVIOUR_THROTTLE_MAX else 0.35
    z_load = (row["engine_load"] - base[b]["load"]) / base[b]["mad_load"]
    z_fuel = (row["fuel_rate"] - base[b]["fuel"]) / base[b]["mad_fuel"]
    z_rpm = (row["rpm"] - base[b]["rpm"]) / base[b]["mad_rpm"]
    return trust * (0.5 * z_load + 0.3 * z_fuel + 0.2 * z_rpm)


def _behaviour_mass(clean, bases, anchor):
    """Behavioural total-mass estimate (kg), anchored at the empty baseline.

    score=0 == the empty baseline == the truck's empty mass (`anchor`: the
    self-calibrated tare or explicit kerb weight -- NOT a low-tail fallback,
    which is polluted on loaded-only runs). The score->mass slope is
    calibrated from physics mass estimates that exist on the SAME driving
    windows/rows (per-vehicle), else from the fleet constant
    BEHAVIOUR_TONNES_PER_SCORE. Behavioural mass = anchor + k * median(score).
    Returns (mass, median_score, calibration): "per-vehicle" | "fleet" | None.
    """
    dt = 5.0
    pairs = []  # (score, physics mass)
    all_scores = []
    for i in range(1, len(clean) - 1):
        s = behavior_score(clean[i], bases)
        if s is None:
            continue
        all_scores.append(s)
        v = clean[i]["speed"] / 3.6
        a = (clean[i + 1]["speed"] - clean[i - 1]["speed"]) / 3.6 / (2 * dt)
        if a < 0.35 or v < 2.0:
            continue
        p_wheels = engine_power_w(clean[i]["fuel_rate"]) * DRIVETRAIN_EFF
        f_tractive = p_wheels / v
        f_aero = 0.5 * RHO_AIR * CDA_DEFAULT * v * v
        m = (f_tractive - f_aero) / (a + G * CRR_DEFAULT)
        if 2000 < m < 120000:
            pairs.append((s, m))
    for w in _windows_from_rows(clean):
        ws = [behavior_score(r, bases) for r in w]
        ws = [s for s in ws if s is not None]
        if len(ws) < 3:
            continue
        m = mass_from_energy_balance(w)
        if m is None:
            acc = [x for x in mass_from_acceleration(w) if 2000 < x < 120000]
            m = statistics.median(acc) if acc else None
        if m:
            pairs.append((statistics.median(ws), m))
    if len(all_scores) < BEHAVIOUR_MIN_SCORES:
        return None, None, None
    med_score = statistics.median(all_scores)
    calib = None
    ks = [(m - anchor) / s for s, m in pairs if s > 0.2 and m > anchor]
    if len(ks) >= BEHAVIOUR_MIN_PAIRS:
        k = statistics.median(ks)
        calib = "per-vehicle"
    elif BEHAVIOUR_TONNES_PER_SCORE > 0:
        k = BEHAVIOUR_TONNES_PER_SCORE * 1000.0  # t/score -> kg/score
        calib = "fleet"
    else:
        return None, med_score, None
    if k <= 0:
        return None, med_score, None
    mass = max(anchor, anchor + k * med_score)
    if not 2000 < mass < 120000:
        return None, med_score, calib
    return mass, med_score, calib
