CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS vehicles (
    intangle_id   BIGINT NOT NULL,
    vehicle_no    TEXT NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    odometer_source TEXT,
    PRIMARY KEY (intangle_id, vehicle_no)
);

ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS odometer_source TEXT;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS payload_tonnes DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS total_mass_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS tare_mass_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS tonnage_note TEXT;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS tonnage_updated_at TIMESTAMPTZ;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS kerb_weight_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS kerb_model TEXT;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS gvw_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS payload_capacity_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS vehicle_type TEXT;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS trailer_weight_kg DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS vehicles_vehicle_no_idx ON vehicles (vehicle_no);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          BIGSERIAL PRIMARY KEY,
    vehicle_no  TEXT NOT NULL,
    day         DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    records     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (vehicle_no, day)
);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telemetry (
    vehicle_id       BIGINT NOT NULL,
    time             TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ,
    tracker_id       BIGINT,
    rpm              DOUBLE PRECISION,
    speed            DOUBLE PRECISION,
    engine_load      DOUBLE PRECISION,
    accel_pedal      DOUBLE PRECISION,
    fuel_rate        DOUBLE PRECISION,
    coolant_temp     DOUBLE PRECISION,
    intake_temp      DOUBLE PRECISION,
    boost_pressure   DOUBLE PRECISION,
    oil_pressure     DOUBLE PRECISION,
    air_inlet_press  DOUBLE PRECISION,
    egt              DOUBLE PRECISION,
    ambient_temp     DOUBLE PRECISION,
    gear             DOUBLE PRECISION,
    odometer         DOUBLE PRECISION,
    engine_hours     DOUBLE PRECISION,
    total_fuel       DOUBLE PRECISION,
    battery_voltage  DOUBLE PRECISION,
    axle_load        DOUBLE PRECISION,
    axle_load_2      DOUBLE PRECISION,
    vehicle_load_cond TEXT,
    lift_axle        TEXT,
    raw              JSONB
);

SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS telemetry_vehicle_time_idx
    ON telemetry (vehicle_id, time DESC);

CREATE TABLE IF NOT EXISTS gps_telemetry (
    vehicle_id  BIGINT,
    vehicle_no  TEXT NOT NULL,
    time        TIMESTAMPTZ NOT NULL,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    raw         JSONB
);

SELECT create_hypertable('gps_telemetry', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS gps_telemetry_vehicle_time_idx
    ON gps_telemetry (vehicle_no, time DESC);

CREATE INDEX IF NOT EXISTS gps_telemetry_vehicle_id_time_idx
    ON gps_telemetry (vehicle_id, time DESC);

CREATE TABLE IF NOT EXISTS driver_profiles (
    id SERIAL PRIMARY KEY,
    driver_name TEXT NOT NULL,
    vehicle_no TEXT,
    profile_data JSONB DEFAULT '{}',
    driving_patterns JSONB DEFAULT '[]',
    route_patterns JSONB DEFAULT '[]',
    trained_at TIMESTAMPTZ,
    confidence FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id     BIGINT PRIMARY KEY,
    vehicle_no      TEXT NOT NULL,
    shipment_type   TEXT,
    net_weight_t    DOUBLE PRECISION,
    shipment_date   DATE NOT NULL,
    party           TEXT,
    vehicle_type    TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vehicle_no, shipment_date, shipment_id)
);

CREATE INDEX IF NOT EXISTS shipments_vehicle_date_idx ON shipments (vehicle_no, shipment_date);

CREATE TABLE IF NOT EXISTS trips (
    vehicle_id      BIGINT NOT NULL,
    vehicle_no      TEXT NOT NULL,
    day             DATE NOT NULL,
    trip_no         INTEGER NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    distance_km     DOUBLE PRECISION,
    duration_min    DOUBLE PRECISION,
    moving_min      DOUBLE PRECISION,
    idle_min        DOUBLE PRECISION,
    avg_speed_kmh   DOUBLE PRECISION,
    max_speed_kmh   DOUBLE PRECISION,
    fuel_used_l     DOUBLE PRECISION,
    fuel_per_km     DOUBLE PRECISION,
    avg_engine_load DOUBLE PRECISION,
    avg_rpm         DOUBLE PRECISION,
    start_lat       DOUBLE PRECISION,
    start_lng       DOUBLE PRECISION,
    end_lat         DOUBLE PRECISION,
    end_lng         DOUBLE PRECISION,
    load_label      TEXT,
    avg_payload_t   DOUBLE PRECISION,
    loaded_frac     DOUBLE PRECISION,
    gap_boundary    BOOLEAN NOT NULL DEFAULT FALSE,
    prev_payload_t  DOUBLE PRECISION,
    next_payload_t  DOUBLE PRECISION,
    trip_type       TEXT,
    start_event     TEXT,
    end_event       TEXT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vehicle_id, day, trip_no)
);

CREATE INDEX IF NOT EXISTS trips_vehicle_day_idx ON trips (vehicle_no, day DESC);
