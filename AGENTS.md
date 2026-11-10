# Balaji Fleet Intangles — Project Reference

Master reference for the fleet telemetry + AI project. Read this first in any session.
Stack: **Python (FastAPI) + TimescaleDB/Postgres + React (.jsx)**. All in this repo (`~/Documents/balaji`).

## Hard constraint: fully in-house / offline

The product must work in-house with **no online AI models or cloud AI services** at runtime.
- No calls to OpenAI/Anthropic/cloud ML APIs, in the app or in scripts.
- All ML (tonnage regression, loaded/empty classifier, driver profiling, forecasting) is **LightGBM (or similar) trained and inferred locally**; models ship inside the repo.
- Every feature (trip segmentation, physics tonnage solver, dashboards) is pure local computation. The dashboard must work with no external dependency.
- The **only external integration is the Intangles OBD API** — the client's own fleet data feed (see §2). That is a data source, not an AI service.

## Workflow rules

- **Never `git push` (or `git commit`) unless the user explicitly asks.** Do the work locally; wait for an explicit "push it".

---

## 1. Project status (as of 2026-08-01)

- **Intangles OBD API fully integrated & live.** 614 vehicles registered, ~425k telemetry rows and growing, ~395 vehicles with data and tonnage estimates.
- Live poller ingests **today's** data every ~60–900 s (background). Backfill pulls **45 days** history in background.
- GPS data integrated — live poller pulls GPS alongside OBD; `gps_telemetry` hypertable populated.
- **Trip recompute automated**: `scripts/trips_loop.py` (in supervisord) recomputes the `trips` table for the last 3 days every 15 min, so the Timetable always shows trip start/end markers + per-trip load labels. Frontend Timetable auto-refreshes today's schedule every 60 s.
- **1-year weighbridge ground-truth ingested (2026-08-11)** for supervised tonnage training (training-only, never shown). See §1b full-year status + §5 ingest_weighbridge.py.
- Live poller ingests **today's** data every 60s (background). Backfill pulls **45 days** history in background.
- GPS data integrated — live poller pulls GPS alongside OBD every 60s; `gps_telemetry` hypertable populated.
- Web dashboard live at `http://localhost:5173` (Vite dev) served against FastAPI `http://localhost:8000`.
- **Built:** tonnage physics solver (v1), behavioural load estimator, kerb override system, spec sheet ingestion, vehicle list ingestion (type + trailer weight), GPS integration, vehicles page redesign (card grid), ML tonnage layer (loaded/empty classifier + per-family calibration factors), driver profiling (behaviour fingerprints + fleet benchmarks + Isolation Forest anomalies), supervised payload regressor + load/unload events (shipment manifest ground truth), **trip segmentation + per-trip empty/loaded leg comparison**.
- **Not built yet:** tonnage regression (physics+ML blend), fuel-burn forecasting, auth hardening (test user exists), client deploy.

## 1b. Project status (as of 2026-08-05)

- 614k+ telemetry rows across 614 vehicles (live poller ingesting today every 60s; backfill 45-day history complete).
- **Trip module fully operational**: `app/trips.py` segments each vehicle-day into trips on >10min gaps, labels each LOADED/EMPTY via the supervised payload regressor (shipment ground truth), and writes to the `trips` table. 15,439+ trips stored fleet-wide (Jul 21–Aug 5). Fleet aggregate: loaded legs 0.268 L/km vs empty 0.252 L/km (+46%).
- **Load/unload dwell windows (2026-08-10)**: trips carry `trip_type` (loaded/empty/partial/unknown), `start_event`/`end_event` (park/stream/gap). The >10min dead-band gap between consecutive trips is treated as the physical loading/unloading window — `dwells_from_rows` emits LOADING (payload step ≥ +10t across the gap) / UNLOADING (≤ −10t) / STOP windows with real start/end times, durations, and a human `why`; `dwell_summary(plate, days)` aggregates them. Exposed via `GET /vehicles/{no}/dwell-events` and inline on `/timetable` (`dwells` array + hour-banded dwell cells). Frontend: Timetable dwell bands + VehicleDetail "Loading / unloading windows (real timing)" panel. Validation on MA1UAARHFT6D80360 2026-08-01: loading 15:30→15:58 (+25.2t), unloading 16:06→20:33 (−26.6t).
- **1-year weighbridge ground truth ingested (2026-08-11)** — **TRAINING-ONLY, NEVER displayed**. Client's 1-year weighbridge/ERP workbook (Google Sheets `VehicleShipment_July2025to2026` → `data/shipments.csv`) loaded into the `shipments` table: **88,669 rows / 469 vehicles / 2025-07-01 → 2026-07-31**, weights 0–69.6t (4 junk >100t rows + 11 zero-weight rows dropped by `scripts/ingest_weighbridge.py`; Excel serial dates converted; plates normalized). **Hard rule: shipment net weights are ground truth for supervised training ONLY — no UI/API endpoint may ever surface them.** They feed `build_regression_dataset`/`evaluate_vs_shipments` and `models/accuracy_history.json` (internal train-loop tracking). **Training condition to reuse:** the regressor is trained from (vehicle, day) shipment days that actually have telemetry; telemetry currently spans only Jul 21→present (~3 weeks), so the effective label coverage is that window even though labels cover a year. Retrain command: `.venv/bin/python -m scripts.train_tonnage_regression --days 400` (default is now 400, was 45).
- **Payload regressor** (LightGBM, 14,999 windows/265 vehicles, trained on the 1-year weighbridge labels): 5-fold CV **MAE 5.24t, R² 0.886**. Deployment-style eval vs the manifest (whole-day OBD prediction → median loaded payload vs actual weight): **MAE 10.21t, median error 5.95t, MAPE 26.1%** over 522 shipment-days (mean weight 45.5t) — this is the honest "live API must equal weighbridge" error, and it is NOT met (target ±100kg). Error is dominated by a cluster of heavy 60-68t days on 2026-07-29 that the model under-reads by 45-60t; best days are within ±0.1t; loaded trips avg 33.6t est vs 45.5t fleet mean weighbridge (under-estimate bias). Added `physics_mass_t` (calibrated physics solver estimate) + `fuel_per_km` features so the model anchors on absolute mass instead of re-deriving scale from fuel rate alone. `build_regression_dataset` filters to `_telemetry_days()` (only shipment-days with telemetry rows) so full-year runs don't scan the year pointlessly. `train_loop` step 1b re-evaluates vs the shipment manifest and appends to `models/accuracy_history.json`. Dwell-trip anchor labelling (forced trip-structure labels on ambiguous windows) was tried and **reverted** — it regressed CV to MAE 7.0t; classifier-confident windows (>0.65 / <0.35) are the better training signal.
- **Loaded/empty classifier**: acc 0.894, AUC 0.958 (30,324 windows/130 vehicles). Calibration factors: truck 1.906, trailer 2.203, global 2.069 (`models/calibration.json`).
- **Tonnage batch** (`scripts/tonnage --days 7`): 537 vehicles cached, 20 skipped (insufficient data), 120 kerb-fallback. Validation vs known shipments: best cases ±2.8t (KA06AB5158, 43t shipment → 40.3t est), but fleet median ~±5-7% due to grade/no-elevation ceiling. **The ±100kg (0.1t) absolute accuracy target is NOT met** — it requires weighbridge ground-truth labels and GPS elevation for grade correction (see §6 notes). Relative payload (loaded vs empty) is reliable.
- **Timetable page**: shows trip start/end markers directly on the 24h grid (no click needed), colored by load label with payload tonnage. Schedule hours spanned by a loaded trip now show "loaded" even during idle portions (trip-aware override) — fixes the discrepancy where a loaded trip with long yard waits would show "no load" in telemetry-absent hours.
- **`supervisord.conf`** added for production process management (API + live poller + backfill + train loop with auto-restart). Use instead of raw `nohup` — the shell-tool backgrounding gets reaped when the parent session ends.
- Processes now managed by supervisord (or manually: see §1 running-processes list).

### How to check status

```bash
cd ~/Documents/balaji
tail backfill.log    # history backfill progress
tail live.log        # live poller (today) progress
tail train.log      # self-training loop (tonnage + driver profiles) progress
curl -s localhost:8000/health   # db + row counts
# DB summary:/opt/homebrew/opt/postgresql@18/bin/psql -d balaji -c "SELECT day, status, count(*) FROM ingest_runs GROUP BY day, status ORDER BY day;"
# GPS data check:
/opt/homebrew/opt/postgresql@18/bin/psql -d balaji -c "SELECT count(*) FROM gps_telemetry;"
# Tonnage cache check:
/opt/homebrew/opt/postgresql@18/bin/psql -d balaji -c "SELECT count(*) FROM vehicles WHERE payload_tonnes IS NOT NULL;"
# Latest calibration factors:
/opt/homebrew/opt/postgresql@18/bin/psql -d balaji -c "SELECT 1;" ; cat models/calibration.json
```

### Running processes (start manually if machine rebooted)

```bash
cd ~/Documents/balaji
.venv/bin/supervisord -c supervisord.conf   # runs: api, backfill(365d), live_poller, train_loop(365d), trips_loop(15min)
# or manually (nohup is fine for an interactive session; use supervisord for long-running):
.venv/bin/python -u -m scripts.backfill --days 365  > backfill.log 2>&1 & disown   # history (full year)
.venv/bin/python -u -m scripts.live --interval 60 --lookback-seconds 900 > live.log 2>&1 & disown        # today's live data (60s interval, 900s lookback)
.venv/bin/python -u -m scripts.train_loop --interval-hours 6 --days 365 > train.log 2>&1 & disown   # self-training: retrains tonnage/loads models + driver profiles, reloads API; --days now 365 (full weighbridge year)
.venv/bin/python -u -m scripts.trips_loop --interval-minutes 15 --days 3 > trips_loop.log 2>&1 & disown # recompute last-3-day trips so Timetable shows markers
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > api.log 2>&1 & disown  # API
cd web && npm run dev > ../vite.log 2>&1 & disown                                  # frontend dev (port 5173)
```

For production: `cd web && npm run build` → serve `web/dist` from FastAPI. Use **supervisord** (`supervisord -c supervisord.conf`) for process management instead of raw `nohup` — `nohup &` gets reaped when the shell session ends; supervisord daemonizes properly and restarts crashed processes. Logs go to `logs/`. See §9 supervisord.conf.

### Infrastructure (installed on this Mac)

- **PostgreSQL 18 + TimescaleDB 2.29** via Homebrew. Server: `brew services start postgresql@18`.
  - TimescaleDB required `shared_preload_libraries='timescaledb'` in `/opt/homebrew/var/postgresql@18/postgresql.conf`.
  - `.so` links were hand-made into `/opt/homebrew/lib/postgresql@18/` (Homebrew resolves `.so`→`.dylib`; the `.dylib` files are in `/opt/homebrew/Cellar/timescaledb/2.29.0/lib/timescaledb/postgresql@18/`).
  - DB name: `balaji`. Default user = macOS user, no password. Connection: `postgresql://localhost:5432/balaji`.
- Python 3.14 venv at `.venv/`. Node 20 + Vite 8 in `web/`.

---

## 2. Intangles OBD API

Base URL: `https://external.balajitransports.in/api`
Credentials: `Intangleuser` / `intangle@12` (in `.env`, gitignored; also documented here for session reference — repo must stay private). The client URL-encodes the password itself — store raw.

### Auth flow (3 steps, no refresh endpoint documented)

1. **Login** → `POST {base}/WebLogin?Username={u}&password={p}` → `{"success":true,"token":"<JWT>","user":{...}}`. Token lives ~1h (exp claim).
2. **GetIntangleVehicles** → `GET {base}/VehicleHealth/GetIntangleVehicles`, header `Authorization: Bearer <JWT>`, `accept: text/plain`.
   → `[{"IntangleId":"<19-digit-snowflake>","Vehicle":"<plate-or-VIN>"}]`. ~605 entries. **Same IntangleId can appear under multiple plates/VINs** (aliases).
3. **GetVehicleOBD** → `GET {base}/IntangleVehicleReports/GetVehicleOBD?vehicleNos=A,B&fromDate=2026-07-21 00:00:00&toDate=2026-07-21 23:59:59.999`, header `Authorization: Bearer <JWT>`.
   - `vehicleNos`: comma-separated plates. Spaces ignored. Multiple vehicles allowed in one call.
   - Dates: date-only spans the full day; date+time restricts the range.

### Response shapes (VERIFIED LIVE — differs from the client's doc)

- `GetVehicleOBD` returns **one object per vehicle**: `[{"status":{"code":200},"results":{"data":[...], "paging":{...}}}]`.
- Each record in `data`:
  - `start_time` / `end_time` = epoch **milliseconds** (end = start + 5000, i.e. **5-second samples**).
  - `protocol: "SAE J1939"`, `pt: 5000`.
  - `vehicle_id` = the **IntangleId** (sometimes serialized as a **string** — must int() it; it is NOT the plate).
  - `tracker_id`, `_id`, `server_timestamp`, `exb`, `refid`, `tags`, `__*` housekeeping fields.
  - `pids`: a **list containing one dict**, keyed by numeric PID → `{"timestamp":<ms>,"value":[<v>]}` (some have `"derived":"..."`).

### Documented API quirks (verified)

- Empty day → HTTP **200** with `results.data: []` (not an error). Handle `[]`, don't treat as failure.
- Unknown vehicle → HTTP **404**. Login-expired → HTTP **401**.
- Transient HTTP **500s** happen; client retries 5xx up to 3×, and `poll()` auto-retries anything not marked `success`/`empty`.
- Vehicles run **intermittently** (a truck may have data 07-21 but nothing 07-22..07-30). `ingest_runs` tracks every (vehicle, day) so polls don't repeat wasted work.
- **`fromDate`/`toDate` are interpreted as UTC wall-clock** (not server/IST time). Full-day queries are safe (24h IST window is a superset when read as UTC), but **short windowed queries must be formatted in UTC** — an IST wall-clock window reads as ~5.5h in the future and returns `data: []`. The live poller hits this; `poll_live_window` always builds the window with `datetime.now(timezone.utc)`. Keep the lookback window larger than the pass duration (~9 min for 606 vehicles) so passes overlap.

---

## 3. Data catalog per vehicle (the PIDs)

`pids[0]` is keyed by numeric IDs. For the main production trucks these follow **J1939 SPN numbers** for the well-known ones. Verified value ranges from live data (Blazo / BS6 trucks):

| PID | Signal (verified) | Notes |
|---|---|---|
| 84 | Wheel speed km/h | 0–70. **Also reported as `mhn_84a`/`mhn_84b` (Blazo alt frame; a/b two channels, ~1 km/h apart) or `spn_84_avg` (OBD2_AL/BS4_EI). One key per 5s sample — never two.** Normalizer falls back 84 → mhn_84a → mhn_84b → spn_84_avg. ~18k rows are status-only/engine-off frames with no speed at all. |
| 91 | Accelerator pedal % | 0–100 |
| 92 | Engine load % | 0–100 |
| 190 | Engine speed RPM | 0–2167 |
| 183 | Fuel rate L/h | 0–46 |
| 110 | Coolant temp °C | 33–97 |
| 102 | Intake manifold temp °C | 23–56 |
| 105 | Intake air temp °C | (≈102) |
| 106 | Air inlet pressure kPa | 74–268 |
| 132 | Turbo boost kPa | 0–231 (on AL units) |
| 100 | Engine oil pressure kPa | 100–592 |
| 173 | Exhaust (EGT) temp °C | 32–531 |
| 171 | Ambient temp °C | 24–36 |
| 245 | Odometer km (cumulative) | also `EMS_245`, `mhn_blazo_245` |
| 247 | Engine hours | also `EMS_247`, `mhn_247` (NOT trip distance) |
| 250 | Total fuel used L | also PID 5054 |
| 899 | Gear position | 0–14 |
| 168 | Battery voltage V | 23.8–27.2 (24V system) |
| 182 | Trip fuel L | 0.5–20.5 |
| 184 | Total fuel used (hi-res) L | 0–125 |

**Load-relevant named (derived) fields — important for the tonnage feature:**
- `mhn_veh_load_cond` — vehicle load-condition flag (weak label for loaded/empty). Verified values: `10` = **loaded** (avg fuel 9.3 L/h), `01` = **empty** (avg fuel 5.7 L/h), `11` = intermediate. Used to sanity-check the physics solver.
- `mhn_lift_axle` — lift axle status (empty-truck indicator).
- `mhn_str_pos` / `mhn_str_neg`, `mhn_rev_gear_stat`, `mhn_sas_stat`, `mhn_regen_status`, `mhn_multimode_status` — behaviour/status bits.

**`OBD2_AL_*` and `BS4_EI_*` devices (test/bench units)** expose a different cleaner schema: averages named `spn_84_avg, spn_91_avg, spn_92_avg, spn_190_avg, spn_132_avg, spn_412_avg, dff_*_avg`, plus **`spn_3250_avg` / `spn_3251_avg` (59–541) — candidate axle-load/weight PGNs**. Worth asking the client whether any production trucks carry axle-load sensors.

Most other numeric IDs are status/hex-config flags; hundreds of distinct PIDs exist but few matter.

### What each parameter actually means (plain language, verified on this fleet)

Each telemetry row is one 5-second sample; the values are the reading **at that instant**.

- **speed (84)** — road speed of the truck, km/h, at the sample instant.
- **rpm (190)** — **engine crankshaft (flywheel-side) speed**, NOT wheel RPM. `0` while moving = coasting in neutral with engine off, or a stale "hold" echo frame (~13% of moving rows show rpm=0, avg fuel 3.1 L/h, no engine_load). "Live" status ≠ engine running — live = the tracker is sending fresh data.
- **engine_load (92)** — **engine load %**: how much of the engine's max torque at that rpm the ECU thinks it is producing. It is a fuel-demand estimate, not air-suspension/gearbox load. Loaded trucks need more torque → this is the primary tonnage feature.
- **accel_pedal (91)** — accelerator pedal position % (driver's foot).
- **fuel_rate (183)** — instantaneous fuel consumption, **litres per hour** (not per km).
- **gear (899)** — current gear position; **0 = neutral**. ~44% of frames are gear 0 (idle/stopped/coasting). A diesel can roll in neutral, so 0-gear + moving is real but brief.
- **odometer (245/EMS_245)** — cumulative km since first registration.
- **engine_hours (247)** — cumulative hours the **engine has been running, INCLUDING all idle** (loading, signals, traffic, engine-on rest). Fleet implied avg `odometer/engine_hours` = 19–52 km/h; 2400 hrs → 75000 km (≈31 km/h) is normal — most of those hours are idle, not driving.
- **coolant_temp (110)** — engine coolant °C at the sample instant.
- **intake_temp (102) / intake air temp (105)** — intake manifold / inlet air °C.
- **boost_pressure (132)** — turbocharger outlet pressure, kPa (kilopascal).
- **air_inlet_press (106)** — pressure entering the intake (pre-turbo / after filter), kPa.
- **oil_pressure (100)** — engine oil pressure, kPa.
- **egt (173)** — exhaust gas temperature °C. **`-273` (absolute zero) = J1939 "sensor not available" sentinel**, a bad-reading flag, not a real temperature; seen only on 2 units (TS05UD4299, AP16TE6393). Production trucks read 32–531 °C.
- **ambient_temp (171)** — outside air temperature °C.
- **battery_voltage (168)** — 24V system voltage. **Only ~1/3 of frames report it** — some firmwares don't emit PID 168; `—` on a vehicle just means absent from its data, not a fault.
- **total_fuel (250/184)** — cumulative litres burned over the truck's lifetime.
- **axle_load / axle_load_2 (spn_3250/3251)** — axle weight PGNs. **Found on a few real production trucks** (KA06AB3591, KA06AB6551 report 36–500 over 3 days), not only test units; most trucks don't carry axle-load sensors → `—`. If present, this is the best tonnage signal available — confirm units/scale + which trucks have it with the client.
- **vehicle_load_cond (mhn_veh_load_cond)** — the truck's own load-condition flag: `10`=loaded, `01`=empty, `11`=intermediate. Weak label used to calibrate/validate the tonnage solver.
- **lift_axle (mhn_lift_axle)** — 0/1 on trucks that HAVE a lift axle (empty-truck indicator); `—` = no lift axle fitted / not reported (correct, not a bug).

### Data-quality notes

- **Live mileage is computable right now** (no new sensor needed): instantaneous km/L = `speed / fuel_rate`; average over a window = `Δodometer / Δtotal_fuel`.
- ~18k rows are status-only/engine-off frames with no speed; ~13% of moving rows are coasting/hold frames (rpm=0). Consumers filter via `app/tonnage.py:_clean()`; raw JSONB always kept intact.

### GPS data (separate 24×7 feed, now integrated)

**GPS data fetching API (separate endpoint, date-only format):**
- Base URL: `{intangles}/IntangleVehicleReports/GetIntanglesVehicleLocation`
- Request params: `vehicleNos` (comma-separated plates), `fromDate` (yyyy-MM-dd), `toDate` (yyyy-MM-dd)
- Response: array of `{intangle_id, vehicle_plate, lat, lng, timestamp(ms), ...}` (5 s samples, epoch ms, decimal degrees)
- Note: endpoint path has a typo in Intangles' API (`GetIntanglesVehicleLocation`, not `GetIntanglesVchicleLocation`)

**No lat/long fields in OBD.** GPS is fetched separately and stored in `gps_telemetry` hypertable on `time`. The live poller pulls GPS alongside OBD every 15 min. Backfill also pulls GPS per day per vehicle. GPS data is used for slope/grade in the physics tonnage solver (future) and for the `gps_stationary_20m` flag on the `/live` endpoint.

No GPS elevation feed yet — slope/grade for the physics tonnage solver requires this or flat-road assumption + ML correction.

See `docs/sample_obd_record.json` for one real record's full `pids` dict.

---

## 4. Database schema (TimescaleDB `balaji`)

- **`vehicles`** — PK `(intangle_id, vehicle_no)`. One row per alias pair (a single IntangleId can map to a VIN + several plates). `is_active`, `first_seen`, `last_seen`. **Tonnage cache columns:** `payload_tonnes`, `total_mass_kg`, `tare_mass_kg`, `tonnage_note` (`ok` | `grade-limited (...)`, `insufficient data/telemetry`), `tonnage_updated_at`. Filled by `python -m scripts.tonnage --days 7` (batch, not per-request). **Kerb columns:** `kerb_weight_kg` + `kerb_model` (per-vehicle manufacturer tare override; set via `scripts/set_kerb.py <csv>` or admin `POST /vehicles/kerb`). **Spec columns:** `gvw_kg`, `payload_capacity_kg` (from client spec sheet; set via `scripts/ingest_specs.py`). **Trailer columns:** `vehicle_type` (e.g. `12 W Truck`, `14 W Trailer`, `18 W Trailer`), `trailer_weight_kg` (6000 for 14W trailer, 8000 for 18W trailer; added to tare for trailers).
- **`ingest_runs`** — PK `(vehicle_no, day)`. `status` ∈ pending/success/empty/error, `records`, `error`. Drives idempotent polling.
- **`users`** — id, `username` (UNIQUE), `password_hash` (bcrypt), `role` (viewer/admin; **first registered user becomes admin**), created_at.
- **`telemetry`** — hypertable on `time`. UNIQUE `(vehicle_id, time)` → idempotent upserts (`ON CONFLICT DO NOTHING`). Columns: vehicle_id, time, end_time, tracker_id, rpm, speed, engine_load, accel_pedal, fuel_rate, coolant_temp, intake_temp, boost_pressure, oil_pressure, air_inlet_press, egt, ambient_temp, gear, odometer, engine_hours, total_fuel, battery_voltage, axle_load, axle_load_2, vehicle_load_cond, lift_axle, **raw (JSONB — full pids, always kept)**.
- **`trips`** — PK `(vehicle_id, day, trip_no)`. One row per segmented trip: start/end (IST day-boundary grouped), distance_km, duration_min, moving_min, idle_min, avg/max speed, fuel_used_l, fuel_per_km, avg_engine_load, avg_rpm, GPS start/end lat/lng, `load_label` (loaded/empty/partial/unknown from the payload regressor), `avg_payload_t`, `loaded_frac`, `gap_boundary`/`prev_payload_t`/`next_payload_t` (dead-band gap context), `trip_type`, `start_event`/`end_event` (park/stream/gap). Idempotent: recompute = DELETE+INSERT per (vehicle_id, day). Filled by `python -m scripts.trips --days N` or admin `POST /ingest/compute-trips`.

Schema SQL: `scripts/init_db.sql`. Create: `python -m scripts.poll` calls `init_schema()`; or via API `POST /ingest/sync-vehicles`.

---

## 5. Code layout

- `app/config.py` — pydantic-settings, reads `.env` (BASE_URL, creds, DATABASE_URL, batch size, delay, lookback).
- `app/intangles.py` — `IntanglesClient`: token cache w/ expiry decode, 401→re-login→retry-once, 5xx retry ×3, `get_vehicles()`, `get_obd()`, `login()`.
- `app/normalize.py` — `PID_COLUMNS` map + `normalize_record()` (raw pids blob → named columns; casts `vehicle_id` to int), `split_by_vehicle()`.
- `app/ingest.py` — `sync_vehicles` (advisory-locked; also re-resolves odometer sources + fixes NULL odometer rows), `ingest_batch` (handles 404→empty, per-vehicle attribution via IntangleId→plate, applies each vehicle's odometer source), `backfill(days)`, `poll()` (yesterday, skips done), `poll_today()` (today, no skip), `_mark_run`.
- `app/odometer.py` — per-vehicle odometer source resolution: `pick_odometer` (source-aware fallback 245→EMS_245→mhn_blazo_245), `resolve_odometer_sources` (scan detects 245 "reset" drops >1000 km → switch that vehicle to EMS_245), `recompute_odometer` (rewrite odometer from raw JSONB; `null_only=True` for the live loop). Serialized via `pg_advisory_xact_lock` so live+backfill don't deadlock.
- `app/db.py` — psycopg3 connection + `init_schema()`.
- `app/security.py` — bcrypt hash/verify, JWT create/decode (HS256, `jwt_secret`/`jwt_expire_minutes` from `.env`).
- `app/auth.py` — `/auth/register` (first user → admin), `/auth/login` (JWT), `/auth/me`, deps `get_current_user` (Bearer header) + `require_admin`.
- `app/main.py` — FastAPI: `/health` (public), `/vehicles`, `/vehicles/{no}/profile` (driver behaviour profile: existing `driver_profiles` row if one exists, else computed on-the-fly read-only, 422 when <12 windows), `/vehicles/{no}/load-events` (per-day LOADED/UNLOADED transitions + payload curve from the regressor), `/vehicles/{no}/dwell-events` (real loading/unloading/stop windows from `dwells_from_rows`; `day` optional, `days>1` → `dwell_summary` aggregate), `/vehicles/{no}/trips` (segmented trips for a day, computes on the fly if not stored), `/vehicles/{no}/trip-comparison` (EMPTY leg vs LOADED leg aggregate over N days: fuel/km, km/L, engine load, rpm, speed + % delta), `/telemetry`, `/telemetry/day-summary` (hourly buckets, time_bucket), `/telemetry/fleet-activity` (fleet-wide hourly moving/idling), `/live` (latest per vehicle via LATERAL), `/live/day` (latest per vehicle within a specific day, for the live-readings table), `/timetable` (per-vehicle 24-hour load schedule for a given IST day, incl. per-hour `dwells` bands + trip start/end event badges), `/tonnage` (physics payload estimate, see §7.2), `/ingest/{poll,backfill,sync-vehicles,resolve-odometer,reload-models,compute-trips}` (**admin only**). All data endpoints require `Authorization: Bearer <JWT>`. CORS open for dev. **Startup `init_schema()` is conditional** (`_schema_ready()` checks `telemetry`+`trips` tables exist) so API restarts don't block on DDL locks held by the live poller/train loop.
- `app/tonnage.py` — physics payload ("virtual weighbridge") solver: `engine_power_w`, `mass_from_energy_balance` (window energy balance), `mass_from_acceleration` (instantaneous force balance), `mass_from_cruise` (steady-speed rolling resistance), `learn_gear_ratios` (per-gear rpm/speed kinematic ratios), `estimate_trip` → `TripEstimate` (robust IQR-trimmed median of pooled estimator outputs, `tare_source` = self-calibrated | **ml-empty** | low-tail | kerb). Accepts `calibration_factor` (learned per-family, overrides the blanket) + `classifier` (LightGBM) + `empty_prob_threshold`; `_classifier_tare` solves classifier-empty sliding windows with the pooled estimator mix, guarded to stay below the load total. **Trailer weight:** `trailer_weight_kg` parameter added to `estimate_trip`; for trailers (14W=6000kg, 18W=8000kg), trailer weight is added to tare mass so payload = total_mass - (kerb + trailer_weight). **Physics calibration** (`TONNAGE_CALIBRATION_FACTOR = 2.0`): the energy-balance solver systematically under-estimates total mass by ~2× because OBD fuel rate under-reports real consumption; calibrated from the client's spec sheet (known unladen weight ÷ solver empty estimate). Applied as a uniform multiplier on all physics mass estimates in `_pooled_masses`. **Behavioural estimator (2026-08-01):** `_empty_reference_rows`/`_build_behavior_baseline` (per-vehicle per-speed-band EMPTY baseline of rpm/engine_load%/fuel_rate from `vehicle_load_cond='01'` rows, else self-calibrated low-throttle/low-load steady rows), `behavior_score` (throttle-controlled deviation from baseline, in baseline-MAD units), `_behaviour_mass` (anchored at self-calibrated tare/kerb, score→tonnes slope calibrated per-vehicle from simultaneous physics-accel rows when ≥15 pairs, else fleet constant `BEHAVIOUR_TONNES_PER_SCORE`=7.0 t/score from KA06AD1203). Behavioural mass blends into the pooled median only when within 0.6–1.6× the physics total; exposed via `/tonnage` as `behaviour_score`/`behaviour_mass_kg`/`behaviour_calib`. Validated: empty ≈ score 0 (fleet median 0.09 on flagged trucks), loaded 0.71 on KA06AD1203. Kerb resolution: explicit `kerb_weight` > `KERB_MODELS[kerb_model]` (client-supplied model→kerb table, populated from spec sheet) > `KERB_WEIGHT_KG[family]` default. **Spec sheet ingestion** (`scripts/ingest_specs.py`): ingests a CSV of vehicle_no, gvw, unladen, payload_capacity, make_model; matches by plate (spaces stripped) → VIN fallback; sets `kerb_weight_kg = unladen`, `gvw_kg`, `payload_capacity_kg`, `kerb_model = make_model`. 39 of 45 sample plates matched (6 Ashok Leyland + 2 later trailers not yet in DB). **Vehicle list ingestion** (`data/vehicle_list.csv`): 631 vehicles with type and trailer weight; 395 matched and ingested into DB with `vehicle_type` and `trailer_weight_kg` columns. Full tonnage batch run: 533 computed, 22 skipped (insufficient telemetry), 248 kerb-fallback.
- `app/trips.py` — **trip segmentation + per-trip empty/loaded comparison + load/unload dwells**: `segment_trips` (split a vehicle-day into trips bounded by >10 min gaps between moving samples — puts the loading-yard wait between the empty A→B and loaded B→C legs), `_trip_stats` (distance via speed integration, duration/moving/idle min, fuel L, fuel_per_km, avg speed/engine load/rpm), `_label_trip` (payload regressor mean ≥ `TRIP_LOADED_T`=10t → loaded, else empty; classifier `loaded_frac` fallback → partial/unknown; short trips fall back to `_light_clean` so they get a payload instead of None), GPS start/end lat/lng via nearest-sample on `gps_telemetry`, `compute_vehicle_trips`/`compute_all` (idempotent DELETE+INSERT per vehicle-day → `trips` table; **always rewrites processed days so stale rows are cleared**), `trips_for_day` (stored, else compute on the fly), `trip_comparison` (aggregate trips per load label over N days + `delta` % between loaded and empty for fuel/km, km/L, load, rpm, speed), **`dwells_from_rows`** (the >10min dead-band gap between consecutive trips = the physical loading/unloading window; payload step ≥ +10t across it → LOADING, ≤ −10t → UNLOADING, else STOP; returns real `start`/`end` datetimes, `duration_min`, `payload_before/after_t`, `delta_t`, `trip_no_before/after`, and a human `why` string), `dwell_events(plate, day)` + `dwell_summary(plate, days)`. Trip labels validated vs known shipment weights (KA06AC4044 39.56t → loaded trips ~38t; KA06AC9527 39.7t → ~42t; empty return leg ~1t).
- `app/tonnage_regression.py` — **supervised payload regression + load/unload event detection**: `build_regression_dataset` (labels = shipment net weight on classifier-loaded windows ≥0.65, 0 on empty ≤0.35, ambiguous dropped; filters to `_telemetry_days()` so only shipment-days with telemetry are scanned; dwell-trip anchor labelling was tried and reverted — regressed CV to MAE 7.0t), `train_regressor` (LightGBM regression, 5-fold CV, `mae≈5.24t / r²≈0.886` on 14,999 windows / 265 vehicles), `load_regressor`/`get_regressor` (process cache), `predict_windows_payload`, `smooth` (rolling median), `load_events` (sliding-window payload curve → LOADED/UNLOADED events via threshold 10t + 4t hysteresis + ≥30 min min-gap; exposes `curve` for the timeline chart), **`evaluate_vs_shipments(days_back, model, verbose)`** (MAE/R² vs the shipment manifest — called by `train_loop` step 1b, history in `models/accuracy_history.json`). Artifact: `models/payload_regressor.txt` (git-tracked). Retrained by `train_loop` + `scripts/train_tonnage_regression.py`.
- `scripts/trips.py` — CLI batch `--days N [--vehicles ...]`; segments + labels + stores trips into the `trips` table (idempotent per vehicle-day). Run nightly / after ingest; API equivalent `POST /ingest/compute-trips`. Fleet run 2026-08-05: 9.8k trips from 6 days (3.4k loaded / 1.8k empty / 1.3k shunting-unknown). Fleet avg: loaded 0.367 L/km vs empty 0.252 L/km (+46%).
- `scripts/ingest_shipments.py` — loads the shipment manifest CSV (shipmentId, vehicle, ShipmentType, NtWt, ShipmentDate, PartyName, VehicleType) into the `shipments` table; idempotent (ON CONFLICT DO NOTHING). Source file `data/shipments.csv` is gitignored (client data). **As of 2026-08-11 this is the FULL-YEAR file** (88,669 rows / 469 vehicles / 2025-07-01 → 2026-07-31, weights 0–69.6t, median 42.6t), regenerated from the client workbook by `scripts/ingest_weighbridge.py`.
- `scripts/ingest_weighbridge.py` — **client's 1-year weighbridge workbook → `shipments` table**. Reads the client Excel (Google Sheets `VehicleShipment_July2025to2026`, one sheet per vehicle: shipmentId, vehicle, ShipmentType, NtWt, ShipmentDate, PartyName, VehicleType), concatenates all sheets, converts Excel serial `ShipmentDate` → ISO date, strips/normalizes plates, drops junk weights (>100t kg-typed entries + zero-weight), writes `data/shipments.csv`, then ingests via `ingest_shipments` (idempotent). Run: `.venv/bin/python -m scripts.ingest_weighbridge --path <xlsx>`. **Training-only — never surface shipment weights in UI/API.**
- `scripts/train_tonnage_regression.py` — CLI `--days N [--skip-classifier]` (**default N=400 = full 1-year weighbridge window**); rebuilds the regression dataset from shipments + telemetry and retrains `models/payload_regressor.txt`.
- `scripts/backfill.py` — CLI `--days N [--vehicles ...] [--include-today]`, **default N=365** (full-year OBD + GPS history; `ingest_runs` skips already-done days, safe to re-run). `--include-today` re-fetches today's full day via the batch-5 path (recovery when the live poller was down).
- `scripts/trips_loop.py` — continuous trip-segmentation loop (`--interval-minutes 15 --days 3`): recomputes the last-3-days `trips` every 15 min so the Timetable always shows trip start/end markers + per-trip load labels automatically (no manual `scripts/trips.py` needed).
- `scripts/poll.py` — CLI daily job.
- `scripts/live.py` — CLI loop `--interval` seconds (default 60) → `poll_today`; `--lookback-seconds 900` (pass takes ~9 min for 606 vehicles).
- `app/ml_tonnage.py` — **self-learning tonnage refinements (fully local, no weighbridge)**: `window_features` (per-window stats of speed/rpm/engine_load/fuel_rate/accel_pedal/gear + derived load_per_rpm/fuel_per_speed/power), `sliding_windows` (fixed 24-row @5s / step 6 — the coarse trip splitter yields too few windows for ML), `build_classifier_dataset` (labels from ECU `vehicle_load_cond` 10/01, majority per window), `train_classifier` (LightGBM binary, 5-fold CV, `acc≈0.91 / auc≈0.97` on 22k windows / 129 vehicles), `load_classifier`, `classify_windows_prob`, `learn_calibration_factors` (**anchored on RTO kerb + trailer weight: factor = (kerb + trailer_weight) ÷ raw-solver-empty-mass; trusted anchors = ECU flag-'01' windows, ML-expanded anchors admitted only within 0.6–1.7× the flag-derived median** because classifier-empty windows skew to light-throttle/coasting the solver under-reads; per-family medians with ≥2 samples, global = fleet median), `calibration_factor_for`, `get_models` (lazy process cache). Artifacts: `models/loaded_empty_lgbm.txt` + `models/calibration.json` (git-tracked, ship in repo). **As of 2026-08-03: truck 1.906, trailer 2.549, global 2.164 (updated by `train_loop`)**. Replaces the blanket 2.0 in the batch + `/tonnage`.
- `app/driver_profiling.py` — **driver profiling (read-only side analysis, NEVER feeds back into tonnage)**: `behavior_features` (extends `window_features` with throttle jerk, hard accel/brake rate, over-rev rate, gear-shift rate, speeding rate, km/L), `_pull_windows` (cleaned sliding windows; `_light_clean` keeps idle rows, drops junk rpm=0-rolling frames), `cluster_styles` (DBSCAN over standardized behaviour features → driving STYLES; -1 = noise), `load_breakdown` (uses the trained loaded/empty classifier via `classify_windows_prob` so behaviour is load-normalized), `fleet_percentiles` + `percentile_rank` (fleet benchmark distribution for km/L), `detect_anomalies` (IsolationForest → anomalous windows with timestamps), `idle_fraction` (day-level engine-on-but-stopped), `profile_vehicle` → `{patterns, profile_data, confidence}` (data-driven confidence from window count + styles + classifier coverage; raises ValueError on <12 windows). Writes only to `driver_profiles` via `POST /driver-profiles/{id}/train?days=N` (real ML, not the old stub).
- `scripts/train_tonnage.py` — CLI `--days N [--skip-classifier]`; daily self-learning run: rebuild classifier dataset → retrain LightGBM → learn calibration factors → writes `models/`. Cron-able; re-run `scripts/tonnage` after so new factors are applied.
- `scripts/train_loop.py` — **background self-training loop (`--interval-hours 6`), keeps everything fresh automatically**: each pass (1) retrains the tonnage classifier + calibration factors, (1b) retrains the supervised payload regressor (shipment ground truth) when ≥50 windows are available, (2) re-trains every driver profile via `retrain_all_profiles` (independent step, never writes tonnage artifacts), (3) POSTs `/ingest/reload-models` on the running API (loop mints its own admin JWT via `app.security.create_token`, same secret) so new artifacts are picked up **without restart**, optionally (4) re-runs the tonnage batch with `--with-tonnage`. `--once` runs a single pass (cron/test). Run with `nohup ... > train.log 2>&1 & disown`.
- `scripts/tonnage.py` — CLI batch `--days N [--vehicles ...] [--limit N]`; per-vehicle physics payload → caches `payload_tonnes`/`total_mass_kg`/`tare_mass_kg`/`tonnage_note` on `vehicles`. Uses stored `kerb_weight_kg` when set; adds `trailer_weight_kg` to tare for trailers (14W=6000kg, 18W=8000kg). Loads `models/calibration.json` + the trained classifier: learned per-family factor replaces the blanket, and the classifier finds empty windows for tare (`tare_source = ml-empty`) on trucks that emit no load flag. Reports how many trucks fell back to family kerb. Idempotent; re-run nightly. Skips trucks with <30 telemetry rows; flags grade-limited (total below tare / implausibly low mass) as NULL payload instead of a misleading 0.
- `scripts/set_kerb.py` — loads a `vehicle_no,model[,kerb_weight_kg]` CSV into `vehicles.kerb_weight_kg`/`kerb_model`. Model-only rows resolve via `app.tonnage.KERB_MODELS` (skipped + reported if the model is unknown). `--dry-run` prints what would change; idempotent.
- `scripts/ingest_specs.py` — loads a spec-sheet CSV (`vehicle_no, vehicle_type, year, reg_date, rto, make, make_model, chassis_no, engine_no, gvw, unladen, payload_capacity`); normalizes plates (strip spaces), matches by plate → fallback VIN; sets `gvw_kg`, `payload_capacity_kg`, `kerb_weight_kg = unladen`, `kerb_model = make_model` on matched vehicles. 39 of 45 sample plates matched on first run.
- `web/` — Vite + React. `src/AuthContext.jsx` (token in localStorage `balaji_token`/`balaji_user`, login/register/logout), `src/pages/Login.jsx` (sign-in/register card), `src/components/Topbar.jsx` (3-col nav: brand / Dashboard / Vehicles links / profile bubble), `src/pages/Dashboard.jsx` (KPI cards), `src/pages/VehiclesLayout.jsx` (center-aligned sub-nav: Timetable | Observation | All Vehicles), `src/pages/Timetable.jsx` (**date-driven 24-hour load schedule grid** — defaults to today IST, date picker in toolbar loads any past day's real per-vehicle per-hour load status from `/timetable?day=YYYY-MM-DD`; "Live readings" button opens a date-pickable table of stored readings from `/live/day?day=YYYY-MM-DD`, auto-refreshing when today selected), `src/pages/Vehicles.jsx` (3-line card grid, premium look with vehicle_type badge + payload chip), `src/pages/VehicleDetail.jsx` (redesigned detail page: header band with plate/type/live-dot/last-seen/payload-vs-capacity pill; status banner surfacing overloads, no-data-today, anomalies; trimmed live KPI row; location & activity panel with day-selectable 24h moving/idling bars; payload panel with payload gauge + tare/trailer/payload/GVW-headroom mass-stack bar + stats; load/unload timeline panel (regressor curve + LOADED/UNLOADED events, day picker); trips panel with EMPTY-leg vs LOADED-leg comparison chips (fuel/km, km/L, load, speed + % delta over 14 days) and the day's trip list (label, times, km, duration, L/km, ~payload); driver behaviour panel from `GET /vehicles/{no}/profile` with pattern bars, load mix, fuel percentile, anomalies; telemetry explorer with speed/RPM/engine-load/fuel lines and km/L + idle stats), `src/pages/Observation.jsx` (driver profiling — create profiles, "Analyze" runs the real behaviour clustering + fleet benchmarks; card shows pattern bars, load-normalized empty/loaded mix, fuel-economy fleet percentile bar, and Isolation Forest anomaly rows), `src/theme.css` (light/dark CSS variables for premium theme), `src/api.js` (BASE = `VITE_API_URL` || `http://localhost:8000`, attaches Bearer token), HashRouter + ProtectedRoute (unauthenticated → `/login`).

---

## 6. Gotchas & decisions (don't re-learn these)

- `vehicle_id` in OBD records can be a **string** — coerce to int before joining to `vehicles`.
- Response has **one object per requested vehicle**, but if an IntangleId has multiple aliases, a single plate may duplicate data → the UNIQUE index makes it idempotent.
- Homebrew `timescaledb` install needed manual `.dylib` symlinks + `shared_preload_libraries` (see §1). Don't rebuild casually.
- Keep the **raw JSONB** column — PID sets differ per vehicle/firmware; future features (and the tonnage solver) need the uncurated payload.
- **Speed is reported under 4 keys, one per 5s sample, mutually exclusive** — `84` (main J1939), `mhn_84a`/`mhn_84b` (Blazo alt frame, a/b ~1 km/h apart), `spn_84_avg` (OBD2_AL/BS4_EI test units). Normalizer resolves `84 → mhn_84a → mhn_84b → spn_84_avg`. ~18k rows are status-only/engine-off frames with genuinely no speed. If speed coverage drops below ~95%, a source was added by Intangles — check `SELECT DISTINCT jsonb_object_keys(raw) FROM telemetry WHERE jsonb_object_keys(raw) ~ '84|speed'`.
- **Odometer is reported under 3 keys that disagree per vehicle** — `245` (standard J1939, default source), `EMS_245` (differs ~4% on many, badly on a few), `mhn_blazo_245` (always equals `245`). 5 vehicles show `245` odometer-reset drops >1000 km and are pinned to `EMS_245`. Source lives in `vehicles.odometer_source`; recompute via `python -m scripts.resolve_odometer` or admin API `POST /ingest/resolve-odometer`. The live loop self-heals NULL odometer rows via `recompute_odometer(null_only=True)`.
- Poll cadence: live poller re-ingests today every 15 min; backfill skips `success`/`empty` days so it can be re-run with a larger `--days` safely.
- **Timetable day boundary** — `/timetable` computes per-vehicle per-hour load status for a given day in **IST** (`AT TIME ZONE 'Asia/Kolkata'`, matches `telemetry`'s stored UTC). Defaults to today IST; any past day is queryable. `vehicle_load_cond` flags (10=loaded, 11=partial, 01=empty) classify hours where present; hours a truck ran without flags fall back to cached `payload_tonnes` (>0 → loaded, else partial); idle/absent hours are `no load`.
- **Junk "hold" frames** — some vehicles emit kinematically impossible frames (speed>5, rpm=0, fuel≈3.7 L/h constant, engine_load NULL). `app/tonnage.py:_clean()` drops them. Normalizer does NOT drop them because the raw JSONB must stay intact; filters belong in consumers.
- **Gear ratios are only kinematic in steady-state** — during shifts and 5s sampling lag the rpm/speed ratio is garbage; `learn_gear_ratios` uses steady windows only. Don't use raw gear+rpm for anything dynamic without filtering.
- **Tonnage validation (2026-08-01)** — solver on KA06AD1203: `vehicle_load_cond='01'` (empty) → 4.7t total, `'10'` (loaded) → 9.7t total. ~5t spread confirms the physics engine separates load. Known residual: grade (no GPS elevation yet) inflates mass on uphills; robust IQR-trimmed median of pooled estimates mitigates. Absolute mass drifts low/negative on empty trucks (kerb 6500 guessed per family) until weighbridge calibration.
- **Calibration learning gotchas (2026-08-03)** — classifier-empty windows skew to light-throttle/coasting and the physics solver under-reads them, so `learn_calibration_factors` admits ML-expanded anchors ONLY within 0.6–1.7× the ECU-flag-derived median (trusted ground truth, no weighbridge). Trailer anchors MUST include `trailer_weight_kg` (solver measures total combination mass) or trailer factors inflate. Only ~7 trucks fleet-wide emit both a kerb weight and `vehicle_load_cond='01'`; the pool grows as daily live data accumulates. After `scripts/train_tonnage`, re-run `scripts/tonnage` and **restart the API** (or `POST /ingest/reload-models`) so `get_models` picks up new artifacts.
- **API restart no longer blocks (2026-08-10)** — startup runs `init_schema()` only when the `telemetry`/`trips` tables are missing (`_schema_ready()`), so restarts skip the full DDL and don't wait on ACCESS EXCLUSIVE locks held by the live poller/train loop. When new artifacts exist, use `POST /ingest/reload-models` instead of a restart.

---

## 7. Roadmap / next phases (not started)

1. **Trip segmentation** — split `telemetry` into trips (ignition/idle detection, distance, start/end) into a `trips` table. **Status (2026-08-05): BUILT** in `app/trips.py` — gap-based segmentation (>10 min between moving samples), per-trip stats + GPS start/end, load-label from the payload regressor, `trips` table, `/vehicles/{no}/trips`, `/vehicles/{no}/trip-comparison`, VehicleDetail trips panel, `scripts/trips.py` + admin `POST /ingest/compute-trips`. Fleet run: 9.8k trips / 6 days; loaded legs 0.367 L/km vs empty 0.252 L/km (+46%).
2. **Physics tonnage solver ("virtual weighbridge")** — longitudinal dynamics:
   `F_tractive = m·a + m·g·sinθ + m·g·Crr + ½ρCdA·v²`
   Fuel rate→power (BSFC) & engine-load%→torque-map for `F_tractive`; mass from low-gear/high-load windows (highest observability); cruise phase via fuel/km shift. Needs **GPS elevation for θ** (or flat-road assumption + ML residual correction). Calibrate Crr/η per truck family (Blazo X48T etc.) with weighbridge samples. **Status (2026-08-01): v1 SOLVED in `app/tonnage.py`** — energy-balance + accel + cruise estimators, IQR-trimmed median, `/tonnage` API + VehicleDetail panel. **Physics calibration** (blanket `TONNAGE_CALIBRATION_FACTOR = 2.0`): the solver systematically under-estimates total mass by ~2× because OBD fuel rate under-reports real consumption; calibrated from the client's spec sheet (known unladen weight ÷ solver empty estimate). Applied as a uniform multiplier on all physics mass estimates. **2026-08-03: learned per-family factors (see `app/ml_tonnage.py`) now replace the blanket** in `scripts/tonnage.py` and `/tonnage` — anchored on RTO kerb (+ trailer weight) against ECU-flag-confirmed empty windows, global 2.289, truck 1.906, trailer 3.019; the trained loaded/empty classifier also provides `ml-empty` tare for trucks without flags. **Behavioural estimator added (2026-08-01):** scores each row by how far its rpm/engine_load%/fuel_rate sit above the truck's own empty-flag baseline at the same speed (throttle-controlled), converting to a tonne estimate via per-vehicle physics-pair calibration or the fleet constant `BEHAVIOUR_TONNES_PER_SCORE` (7.0 t/score, from KA06AD1203: 4.6t empty → 9.1t loaded over score 0 → 0.72). Validated: empty ≈ score 0 (fleet median 0.09 on flagged trucks), loaded 0.71. Blends only within 0.6–1.6× physics total, so physics stays primary; trucks without empty flags score ~2.3 (fallback baseline = lightest-loaded, not true empty) and are rejected by the gate. **Spec sheet ingestion** (`scripts/ingest_specs.py`): ingests a CSV of vehicle_no, gvw, unladen, payload_capacity, make_model; matches by plate (spaces stripped) → VIN fallback; sets `kerb_weight_kg = unladen`, `gvw_kg`, `payload_capacity_kg`, `kerb_model = make_model`. 39 of 45 sample plates matched (6 Ashok Leyland + 2 later trailers not yet in DB). **Vehicle list ingestion** (`data/vehicle_list.csv`): 631 vehicles with type and trailer weight; 395 matched and ingested into DB with `vehicle_type` and `trailer_weight_kg` columns. Full tonnage batch run: 533 computed, 22 skipped (insufficient telemetry), 248 kerb-fallback. **Next:** per-trip runs (needs trip segmentation), grade from GPS, family kerb/Crr calibration, weighbridge labels to firm the score→tonnes constant.
3. **ML layer** — LightGBM: **loaded/empty classifier BUILT (2026-08-03, see `app/ml_tonnage.py`, acc≈0.91/auc≈0.97 on 22k windows)**; **per-family physics calibration factors BUILT (truck 1.906, trailer 2.549, global 2.164, auto-refreshed by `train_loop`)** replacing the blanket 2.0; **driver profiling BUILT (2026-08-03, see `app/driver_profiling.py` — DBSCAN style clustering, fleet benchmarks, Isolation Forest anomalies, load-normalized via the classifier, real `POST /driver-profiles/{id}/train`);** remaining: tonnage regression (blend with physics), fuel-burn forecasting per driver-vehicle.
4. **Product** — auth/login, client deploy (`web/dist` static + FastAPI), per-trip tonnage badges in the 24h timeline modal.
- **Status (2026-08-05):** Timetable page now shows trip start/end markers directly on the 24h grid (no click). Trip-aware schedule: hours spanned by a LOADED trip show "loaded" even during idle/yard-wait portions (see §6 trip-aware rule). Gap-boundary inference: dead-band gaps (>10min no-data) between trips are tagged `gap_boundary`, and `prev_payload_t`/`next_payload_t` enable lading-vs-unloading inference (payload step ≥10t across a gap = lading; step ≤-10t = unloading).

### 9. Production process management (supervisord)

`supervisord.conf` at the repo root supervises four programs: `api`, `live_poller`, `backfill`, `train_loop`. Install with `pip install supervisor`, then:

```bash
cd ~/Documents/balaji
supervisord -c supervisord.conf        # start all services (daemonized, auto-restart on crash)
supervisorctl -c supervisord.conf status   # check health
supervisorctl -c supervisord.conf restart api   # restart just the API
```

Why not `nohup &`? The shell-tool's background processes (`nohup ... & disown`) are reaped when the shell session terminates — they work for a live interactive session but get SIGKILL'd on shell teardown. supervisord daemonizes into its own session group (PID 1 for its children) so it survives shell exits and auto-restarts crashed services. Logs go to `logs/{api,live,backfill,train}.log`.

**Accuracy note (timetable + tonnage):** the ±100kg (0.1t) absolute tonnage target is **NOT achievable** with OBD telemetry alone — the physics solver can't see load directly and there are **0 vehicles with axle-load sensors** (`spn_3250/3251` = 0) and no GPS elevation for grade correction. Honest deployment-style error vs the weighbridge manifest (whole-day OBD→median payload vs actual weight): **MAE 10.21t, median 5.95t, MAPE 26.1%** over 522 shipment-days — that's ~±13% of the 45.5t fleet mean, not 0.2%. The timetable trip labels are reliable for loaded-vs-empty *comparison*; absolute payload is approximate. Expecting ≤0.2% (≤~90kg) on a 45t load is only possible with real axle-load sensors or a weighbridge-instrumented subset (which we now have for training, and the regressor is already trained on the full 1-year manifest). The trip load labels DO show the right behaviour signature fleet-wide (loaded trips 49.1% avg engine load / 0.371 L/km / 31.8 km/h vs empty 40.5% / 0.227 L/km / 38.1 km/h).

**Data blockers to raise with the client:** (a) ~~ground-truth tonnage labels~~ **RESOLVED 2026-08-11** — 1-year weighbridge/ERP manifest ingested (88,669 shipments / 469 vehicles / Jul 2025→Jul 2026, training-only); (b) GPS elevation feed for grade, (c) how far back history exists / whether axle-load sensors exist on production trucks.
