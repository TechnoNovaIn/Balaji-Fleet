import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  CartesianGrid, BarChart, Bar,
} from 'recharts';
import { getJson, fmtTime, fmtNum } from '../api';

const RANGES = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 24 * 7 },
];

const PROFILE_PATTERNS = [
  ['Hard Acceleration', 'hard_accel_rate'],
  ['Harsh Braking', 'harsh_brake_rate'],
  ['Over-revving', 'over_rev_rate'],
  ['Speeding', 'speeding_rate'],
];

function dayKey(offset = 0) {
  const d = new Date();
  d.setDate(d.getDate() - offset);
  return d.toISOString().slice(0, 10);
}

function fmtGps(row) {
  if (!row || row.lat === null || row.lat === undefined || row.lon === null || row.lon === undefined) return '—';
  return `${fmtNum(row.lat, 5)}, ${fmtNum(row.lon, 5)}`;
}

function LoadPill({ payload, capacity }) {
  if (payload == null) return null;
  const pct = capacity ? Math.min(100, (payload / capacity) * 100) : null;
  const tone = pct == null ? '' : pct > 95 ? 'over' : pct > 70 ? 'high' : 'ok';
  return (
    <span className={`load-pill ${tone}`}>
      {fmtNum(payload, 1)} t{pct != null && <em>· {Math.round(pct)}% cap</em>}
    </span>
  );
}

function StatusBanner({ live, tonnage, profile, hasDataToday }) {
  const items = [];
  if (tonnage?.payload_capacity_kg && tonnage?.payload_tonnes != null &&
      tonnage.payload_tonnes * 1000 > tonnage.payload_capacity_kg) {
    items.push({ tone: 'danger', text: `Payload ${fmtNum(tonnage.payload_tonnes, 1)}t exceeds capacity ${fmtNum(tonnage.payload_capacity_kg / 1000, 1)}t` });
  }
  if (live && live.vehicle_load_cond === '01' && live.gps_stationary_20m) {
    items.push({ tone: 'info', text: 'Empty & stationary — possibly waiting for a load' });
  }
  const anoms = profile?.profile_data?.anomalies;
  if (anoms?.length) {
    items.push({ tone: 'warn', text: `${anoms.length} anomalous driving windows flagged in the last analysis` });
  }
  if (!hasDataToday) {
    items.push({ tone: 'warn', text: 'No telemetry today — truck may be off the road' });
  }
  if (!items.length) return null;
  return (
    <div className="status-banner">
      {items.map((it, i) => (
        <div key={i} className={`status-item ${it.tone}`}><span className="si-dot" />{it.text}</div>
      ))}
    </div>
  );
}

function PayloadBar({ tonnage }) {
  if (!tonnage || tonnage.payload_tonnes == null) {
    return (
      <div className="stats">
        <span>{tonnage?.note || 'Not enough driving data to estimate payload'}</span>
      </div>
    );
  }
  const payloadKg = tonnage.payload_tonnes * 1000;
  const tare = tonnage.tare_mass_kg || 0;
  const trailer = tonnage.trailer_weight_kg || 0;
  const total = tonnage.total_mass_kg || (tare + payloadKg);
  const gvw = tonnage.gvw_kg;
  const capacity = tonnage.payload_capacity_kg;
  const payloadPct = capacity ? Math.min(100, (payloadKg / capacity) * 100) : null;
  const gvwPct = gvw ? Math.min(100, (total / gvw) * 100) : null;
  return (
    <div className="payload-wrap">
      <div className="payload-summary">
        <div className="payload-big">
          <span className="payload-big-num">{fmtNum(tonnage.payload_tonnes, 2)}</span>
          <span className="payload-big-unit">tonnes payload</span>
        </div>
        {capacity != null && payloadPct != null && (
          <div className={`payload-gauge ${payloadPct > 95 ? 'over' : payloadPct > 70 ? 'high' : ''}`}>
            <span className="payload-gauge-fill" style={{ width: `${payloadPct}%` }} />
          </div>
        )}
        {payloadPct != null && <div className="payload-pct">{Math.round(payloadPct)}% of {fmtNum(capacity / 1000, 1)}t capacity</div>}
      </div>

      {gvw != null && (
        <div className="mass-stack">
          <div className="mass-stack-bar">
            {tare > 0 && <span className="ms-seg ms-tare" style={{ width: `${Math.min(100, (tare / gvw) * 100)}%` }} title={`Tare ${fmtNum(tare)} kg`} />}
            {trailer > 0 && <span className="ms-seg ms-trailer" style={{ width: `${Math.min(100, (trailer / gvw) * 100)}%` }} title={`Trailer ${fmtNum(trailer)} kg`} />}
            <span className="ms-seg ms-payload" style={{ width: `${Math.min(100, (payloadKg / gvw) * 100)}%` }} title={`Payload ${fmtNum(payloadKg)} kg`} />
            <span className="ms-seg ms-headroom" style={{ width: `${Math.max(0, Math.min(100, 100 - (total / gvw) * 100))}%` }} title="Remaining GVW headroom" />
          </div>
          <div className="mass-stack-legend">
            {tare > 0 && <span><i className="ms-key ms-tare" />Tare {fmtNum(tare / 1000, 1)}t</span>}
            {trailer > 0 && <span><i className="ms-key ms-trailer" />Trailer {fmtNum(trailer / 1000, 1)}t</span>}
            <span><i className="ms-key ms-payload" />Payload {fmtNum(payloadKg / 1000, 1)}t</span>
            {gvwPct != null && <span><i className="ms-key ms-headroom" />Headroom {fmtNum(Math.max(0, gvw - total) / 1000, 1)}t</span>}
          </div>
        </div>
      )}

      <div className="stats">
        <span>Total mass {fmtNum(total)} kg</span>
        <span>Tare {fmtNum(tare)} kg{tonnage.tare_source ? ` (${tonnage.tare_source})` : ''}</span>
        {tonnage.kerb_weight ? (
          <span>Kerb {fmtNum(tonnage.kerb_weight)} kg{tonnage.kerb_model ? ` (${tonnage.kerb_model})` : ''}</span>
        ) : null}
        {gvw != null && <span>GVW {fmtNum(gvw)} kg</span>}
        <span>Windows {tonnage.windows_used}</span>
        <span>Method {tonnage.method}</span>
        {tonnage.behaviour_score != null && (
          <span>Behaviour {fmtNum(tonnage.behaviour_score, 2)}
            {tonnage.behaviour_mass_kg ? ` → ${fmtNum(tonnage.behaviour_mass_kg)} kg` : ''}</span>
        )}
      </div>
    </div>
  );
}

function DriverPanel({ profile, loading }) {
  const d = profile?.profile_data || {};
  const sum = d.summary || {};
  const lb = d.load_breakdown || {};
  const fuelPct = d.fleet_fuel_percentile;
  const anoms = d.anomalies || [];
  const patterns = profile?.patterns || [];

  if (loading) return <div className="skeleton" style={{ height: 140 }} />;
  if (!profile || profile.error) {
    return <div className="muted" style={{ fontSize: 13 }}>No driver profile yet — create one in Observation to see behaviour fingerprint.</div>;
  }

  return (
    <div className="driver-panel">
      <div className="driver-kpis">
        <div className="pd-kpi"><span className="pd-kpi-v">{sum.windows ?? '—'}</span><span className="pd-kpi-l">windows</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{fmtNum(sum.kmh_avg, 1)}</span><span className="pd-kpi-l">avg km/h</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{sum.styles ?? '—'}</span><span className="pd-kpi-l">styles</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{fmtNum(d.km_per_l, 1)}</span><span className="pd-kpi-l">km/l</span></div>
        {fuelPct != null && <div className="pd-kpi"><span className="pd-kpi-v">{fuelPct}<small>th</small></span><span className="pd-kpi-l">fuel pctile</span></div>}
      </div>

      <div className="driver-patterns">
        {patterns.filter(p => PROFILE_PATTERNS.some(([label]) => label === p.type)).map(p => (
          <div key={p.type} className="dp-item">
            <div className="dp-head"><span>{p.type}</span><b>{Math.round(p.value)}%</b></div>
            <div className="dp-bar"><span className="dp-fill" style={{ width: `${Math.min(100, p.value)}%` }} /></div>
          </div>
        ))}
        {patterns.filter(p => p.type === 'Idling').map(p => (
          <div key={p.type} className="dp-item">
            <div className="dp-head"><span>Idling</span><b>{Math.round(p.value)}%</b></div>
            <div className="dp-bar"><span className="dp-fill" style={{ width: `${Math.min(100, p.value)}%` }} /></div>
          </div>
        ))}
        {patterns.filter(p => p.type === 'Throttle Jerk').map(p => (
          <div key={p.type} className="dp-item">
            <div className="dp-head"><span>Throttle Jerk</span><b>{Math.round(p.value)}%</b></div>
            <div className="dp-bar"><span className="dp-fill" style={{ width: `${Math.min(100, p.value)}%` }} /></div>
          </div>
        ))}
      </div>

      {lb.loaded != null && (
        <div className="pd-load">
          <span className="pd-load-label">Load mix (classifier):</span>
          <div className="pd-load-bar">
            <span className="pd-load-seg pd-load-empty" style={{ width: `${(lb.empty || 0) * 100}%` }} title={`empty ${Math.round((lb.empty || 0) * 100)}%`} />
            <span className="pd-load-seg pd-load-loaded" style={{ width: `${(lb.loaded || 0) * 100}%` }} title={`loaded ${Math.round((lb.loaded || 0) * 100)}%`} />
          </div>
          <span className="pd-load-nums">{Math.round((lb.empty || 0) * 100)}% empty · {Math.round((lb.loaded || 0) * 100)}% loaded</span>
        </div>
      )}

      {anoms.length > 0 && (
        <div className="anomaly-list">
          <div className="anomaly-title">Anomalous windows</div>
          {anoms.slice(0, 4).map((a, i) => (
            <div key={i} className="anomaly-row">
              <span className="anomaly-time">{fmtTime(a.time)}</span>
              <span>rpm {fmtNum(a.rpm)}</span>
              <span>fuel {fmtNum(a.fuel_rate, 1)} L/h</span>
              <span className="anomaly-score">score {a.score}</span>
            </div>
          ))}
        </div>
      )}

      {profile.trained_at && (
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          {profile.driver_name ? `Profile "${profile.driver_name}" · ` : ''}analyzed {fmtTime(profile.trained_at)}
          {profile.confidence ? ` · ${Math.round(profile.confidence * 100)}% conf` : ''}
        </div>
      )}
    </div>
  );
}

function LoadEventsPanel({ events, curve, dwells, loading, error, day, onDay }) {
  if (loading) return <div className="skeleton" style={{ height: 140 }} />;
  return (
    <div className="load-events-panel">
      <div className="panel-head">
        <h2>Load / unload timeline</h2>
        <div className="range">
          {[0, 1, 2].map((off) => (
            <button key={off} className={day === dayKey(off) ? 'active' : ''} onClick={() => onDay(dayKey(off))}>
              {dayKey(off)}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="muted" style={{ fontSize: 13 }}>Load events unavailable ({error})</div>}

      {!error && curve && curve.length > 0 && (
        <div className="chart">
          <ResponsiveContainer width="100%" height={140}>
            <LineChart data={curve}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
              <XAxis dataKey="time" tickFormatter={(t) => new Date(t).toLocaleTimeString()} stroke="var(--muted)" />
              <YAxis stroke="var(--muted)" />
              <Tooltip
                labelFormatter={(t) => new Date(t).toLocaleString()}
                formatter={(v) => [fmtNum(v, 1), 'payload t']}
                contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', color: 'var(--text)' }}
              />
              <Line type="monotone" dataKey="payload_t" stroke="var(--accent)" dot={false} strokeWidth={2} name="Payload t" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="le-list">
        {events && events.length > 0 ? (
          events.map((e, i) => (
            <div key={i} className={`le-row le-${e.type}`}>
              <span className={`le-tag ${e.type}`}>{e.type}</span>
              <span className="le-time">{fmtTime(e.time)}</span>
              <span className="le-payload">~{fmtNum(e.payload_t, 1)} t</span>
            </div>
          ))
        ) : (
          !error && <div className="muted" style={{ fontSize: 13 }}>No load/unload events detected for this day.</div>
        )}
      </div>

      <h3 className="panel-sub">Loading / unloading windows (real timing)</h3>
      {dwells && dwells.events && dwells.events.length > 0 ? (
        <div className="le-list">
          {dwells.events.map((d, i) => (
            <div key={i} className={`le-row le-${d.type === 'stop' ? 'idle' : d.type}`}>
              <span className={`le-tag ${d.type === 'stop' ? 'idle' : d.type}`}>{d.type}</span>
              <span className="le-time">
                {fmtTime(d.start)} → {fmtTime(d.end)}
              </span>
              <span className="le-payload">{Math.round(d.duration_min)} min{d.delta_t != null ? ` · Δ${fmtNum(d.delta_t, 1)}t` : ''}</span>
              <span className="le-why">{d.why}</span>
            </div>
          ))}
        </div>
      ) : (
        !error && <div className="muted" style={{ fontSize: 13 }}>No loading/unloading windows found for this day.</div>
      )}
    </div>
  );
}

function TripsPanel({ trips, comparison, loading, day, onDay }) {
  if (loading) return <div className="skeleton" style={{ height: 140 }} />;
  const cmp = comparison?.error ? null : comparison;
  const loaded = cmp?.loaded;
  const empty = cmp?.empty;
  const delta = cmp?.delta;

  return (
    <div className="trips-panel">
      <div className="panel-head">
        <h2>Trips (segmented) <small>{cmp ? `${cmp.n_trips_other ?? 0} unlabelled excluded` : ''}</small></h2>
        <div className="range">
          {[0, 1, 2].map((off) => (
            <button key={off} className={day === dayKey(off) ? 'active' : ''} onClick={() => onDay(dayKey(off))}>
              {dayKey(off)}
            </button>
          ))}
        </div>
      </div>

      {loaded && empty && delta && (
        <div className="cmp-row">
          <span className="cmp-col">
            <b className="cmp-lbl">EMPTY leg (A→B)</b>
            <span>{empty.n_trips} trips · {fmtNum(empty.avg_distance_km)} km avg</span>
            <span className="cmp-metric">{fmtNum(empty.fuel_per_km, 3)} L/km · {fmtNum(empty.km_per_l, 2)} km/L</span>
            <span className="muted">load {fmtNum(empty.avg_engine_load)}% · {fmtNum(empty.avg_speed_kmh)} km/h</span>
          </span>
          <span className="cmp-delta">
            {delta.fuel_per_km_pct != null && (
              <span className={`d ${delta.fuel_per_km_pct > 0 ? 'up' : ''}`}>fuel/km {delta.fuel_per_km_pct > 0 ? '+' : ''}{delta.fuel_per_km_pct}%</span>
            )}
            {delta.km_per_l_pct != null && (
              <span className={`d ${delta.km_per_l_pct < 0 ? 'down' : ''}`}>km/L {delta.km_per_l_pct > 0 ? '+' : ''}{delta.km_per_l_pct}%</span>
            )}
          </span>
          <span className="cmp-col">
            <b className="cmp-lbl">LOADED leg (B→C)</b>
            <span>{loaded.n_trips} trips · {fmtNum(loaded.avg_distance_km)} km avg</span>
            <span className="cmp-metric">{fmtNum(loaded.fuel_per_km, 3)} L/km · {fmtNum(loaded.km_per_l, 2)} km/L</span>
            <span className="muted">load {fmtNum(loaded.avg_engine_load)}% · {fmtNum(loaded.avg_speed_kmh)} km/h</span>
          </span>
        </div>
      )}
      {!cmp && <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>Not enough loaded + empty trips yet to compare legs.</div>}

      {trips?.error ? (
        <div className="muted" style={{ fontSize: 13 }}>Trips unavailable ({trips.error})</div>
      ) : Array.isArray(trips) && trips.length > 0 ? (
        <div className="trip-list">
          {trips.map((t) => (
            <div key={t.trip_no} className={`trip-row t-${t.load_label}`}>
              <span className={`le-tag ${t.load_label === 'loaded' ? 'loaded' : t.load_label === 'empty' ? 'unloaded' : ''}`}>
                {t.load_label}
              </span>
              <span className="trip-time">{fmtTime(t.start_time)} → {fmtTime(t.end_time)}</span>
              <span className="trip-stats">
                {fmtNum(t.distance_km)} km · {fmtNum(t.duration_min)} min · {fmtNum(t.fuel_per_km, 3)} L/km
                {t.avg_engine_load != null ? ` · ${fmtNum(t.avg_engine_load)}% load` : ''}
                {t.avg_payload_t != null ? ` · ~${fmtNum(t.avg_payload_t, 1)} t` : ''}
              </span>
            </div>
          ))}
        </div>
      ) : (
        !trips?.error && <div className="muted" style={{ fontSize: 13 }}>No trips on this day.</div>
      )}
    </div>
  );
}

export default function VehicleDetail() {
  const { vehicleNo } = useParams();
  const plate = decodeURIComponent(vehicleNo);
  const [live, setLive] = useState(null);
  const [rangeHours, setRangeHours] = useState(6);
  const [series, setSeries] = useState([]);
  const [timelineDay, setTimelineDay] = useState(dayKey());
  const [timeline, setTimeline] = useState([]);
  const [tonnage, setTonnage] = useState(null);
  const [profile, setProfile] = useState(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [loadEvents, setLoadEvents] = useState(null);
  const [dwells, setDwells] = useState(null);
  const [loadEventsDay, setLoadEventsDay] = useState(dayKey());
  const [trips, setTrips] = useState(null);
  const [tripsDay, setTripsDay] = useState(dayKey());
  const [comparison, setComparison] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function loadLive() {
      try {
        const data = await getJson('/live?minutes=20');
        const found = data.find((r) => r.vehicle_no === plate);
        if (!cancelled) setLive(found || null);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    loadLive();
    const id = setInterval(loadLive, 10000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [plate]);

  useEffect(() => {
    let cancelled = false;
    async function loadSeries() {
      const to = new Date();
      const from = new Date(to.getTime() - rangeHours * 3600 * 1000);
      try {
        const data = await getJson(
          `/telemetry?vehicle_no=${encodeURIComponent(plate)}` +
          `&from_dt=${from.toISOString()}&to_dt=${to.toISOString()}&limit=3000`
        );
        const pts = data.map((r) => ({
          time: new Date(r.time).getTime(),
          speed: r.speed,
          rpm: r.rpm,
          load: r.engine_load,
          fuel: r.fuel_rate,
          gear: r.gear,
        }));
        if (!cancelled) setSeries(pts);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    loadSeries();
    const id = setInterval(loadSeries, 60000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [plate, rangeHours]);

  useEffect(() => {
    let cancelled = false;
    async function loadTimeline() {
      try {
        const data = await getJson(
          `/telemetry/day-summary?vehicle_no=${encodeURIComponent(plate)}&day=${timelineDay}`
        );
        const pts = data.map((r) => ({
          label: r.hour,
          avg_speed: r.avg_speed,
          idle_hours: r.idle_records * 5 / 3600,
          moving_hours: r.moving_records * 5 / 3600,
        }));
        if (!cancelled) setTimeline(pts);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    loadTimeline();
    return () => {
      cancelled = true;
    };
  }, [plate, timelineDay]);

  useEffect(() => {
    let cancelled = false;
    async function loadTonnage() {
      try {
        const data = await getJson(`/tonnage?vehicle_no=${encodeURIComponent(plate)}&days=3`);
        if (!cancelled) setTonnage(data);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    loadTonnage();
    const id = setInterval(loadTonnage, 300000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [plate]);

  useEffect(() => {
    let cancelled = false;
    async function loadProfile() {
      setProfileLoading(true);
      try {
        const data = await getJson(`/vehicles/${encodeURIComponent(plate)}/profile`);
        if (!cancelled) setProfile(data);
      } catch (e) {
        if (!cancelled) setProfile({ error: true, message: String(e) });
      } finally {
        if (!cancelled) setProfileLoading(false);
      }
    }
    loadProfile();
    return () => {
      cancelled = true;
    };
  }, [plate]);

  useEffect(() => {
    let cancelled = false;
    async function loadLoadEvents() {
      try {
        const data = await getJson(
          `/vehicles/${encodeURIComponent(plate)}/load-events?day=${loadEventsDay}`
        );
        if (!cancelled) setLoadEvents(data);
      } catch (e) {
        if (!cancelled) setLoadEvents({ error: String(e), events: [], curve: [] });
      }
    }
    async function loadDwells() {
      try {
        const data = await getJson(
          `/vehicles/${encodeURIComponent(plate)}/dwell-events?day=${loadEventsDay}`
        );
        if (!cancelled) setDwells(data);
      } catch {
        if (!cancelled) setDwells(null);
      }
    }
    loadLoadEvents();
    loadDwells();
    return () => {
      cancelled = true;
    };
  }, [plate, loadEventsDay]);

  useEffect(() => {
    let cancelled = false;
    async function loadTrips() {
      try {
        const data = await getJson(
          `/vehicles/${encodeURIComponent(plate)}/trips?day=${tripsDay}`
        );
        if (!cancelled) setTrips(data);
      } catch (e) {
        if (!cancelled) setTrips({ error: String(e) });
      }
    }
    loadTrips();
    return () => {
      cancelled = true;
    };
  }, [plate, tripsDay]);

  useEffect(() => {
    let cancelled = false;
    async function loadComparison() {
      try {
        const data = await getJson(
          `/vehicles/${encodeURIComponent(plate)}/trip-comparison?days=14`
        );
        if (!cancelled) setComparison(data);
      } catch (e) {
        if (!cancelled) setComparison({ error: String(e) });
      }
    }
    loadComparison();
    return () => {
      cancelled = true;
    };
  }, [plate]);

  const stats = useMemo(() => {
    if (!series.length) return null;
    const moving = series.filter((p) => p.speed > 1);
    const avg = (arr, k) => (arr.length ? arr.reduce((s, p) => s + (p[k] || 0), 0) / arr.length : 0);
    const kml = [];
    for (const p of series) {
      if (p.speed > 5 && p.fuel > 0) kml.push(p.speed / p.fuel);
    }
    const idle = series.filter((p) => p.speed <= 1 && p.rpm > 0).length;
    return {
      avgSpeed: avg(moving, 'speed'),
      maxSpeed: Math.max(...series.map((p) => p.speed || 0)),
      avgLoad: avg(series, 'load'),
      avgRpm: avg(series, 'rpm'),
      kml: kml.length ? kml.reduce((a, b) => a + b, 0) / kml.length : null,
      idleFrac: series.length ? (idle / series.length) * 100 : null,
    };
  }, [series]);

  const hasDataToday = useMemo(() => {
    if (!live) return false;
    const t = new Date(live.obd_time || live.time || 0);
    return (Date.now() - t.getTime()) < 24 * 3600 * 1000;
  }, [live]);

  const capacityT = tonnage?.payload_capacity_kg ? tonnage.payload_capacity_kg / 1000 : null;

  return (
    <div className="detail-page">
      <header className="detail-head">
        <Link to="/vehicles" className="back">← Vehicles</Link>
        <div className="plate-wrap">
          <h1 className="plate">{plate}</h1>
          {tonnage?.vehicle_type && <span className="badge vtype-badge">{tonnage.vehicle_type}</span>}
        </div>
        <span className={`dot ${live && !live.stopped ? 'live' : 'stopped'}`}>
          {live ? (live.stopped ? 'stopped' : 'live') : '—'}
        </span>
        {live && <span className="last-seen">last OBD {fmtTime(live.obd_time || live.time)}</span>}
        {capacityT != null && tonnage?.payload_tonnes != null && (
          <LoadPill payload={tonnage.payload_tonnes} capacity={capacityT} />
        )}
      </header>

      {error && <div className="banner">API unreachable ({error})</div>}

      <StatusBanner live={live} tonnage={tonnage} profile={profile} hasDataToday={hasDataToday} />

      <section className="cards">
        <div className="card card-speed">
          <span className="label">Speed</span>
          <b>{fmtNum(live?.speed, 1)}<small> km/h</small></b>
        </div>
        <div className="card"><span className="label">RPM</span><b>{fmtNum(live?.rpm)}</b></div>
        <div className="card"><span className="label">Engine load</span><b>{fmtNum(live?.engine_load)}<small>%</small></b></div>
        <div className="card"><span className="label">Fuel rate</span><b>{fmtNum(live?.fuel_rate, 1)}<small> L/h</small></b></div>
        <div className="card"><span className="label">Gear</span><b>{fmtNum(live?.gear)}</b></div>
        <div className="card"><span className="label">Odometer</span><b>{fmtNum(live?.odometer, 1)}<small> km</small></b></div>
      </section>

      <section className="panel panel-loc">
        <div className="panel-head">
          <h2>Location &amp; activity</h2>
          <span className="muted loc-sub">{fmtGps(live)}</span>
        </div>
        <div className="loc-chips">
          <span className="chip">GPS {fmtTime(live?.gps_time)}</span>
          {live?.gps_stationary_20m ? <span className="chip">stationary 20m</span> : null}
          {live?.vehicle_load_cond === '01' && <span className="chip">load flag: empty</span>}
          {live?.vehicle_load_cond === '10' && <span className="chip">load flag: loaded</span>}
          {live?.lift_axle && <span className="chip">lift axle {live.lift_axle}</span>}
          {live?.total_fuel != null && <span className="chip">total fuel {fmtNum(live.total_fuel, 0)} L</span>}
          {live?.engine_hours != null && <span className="chip">engine {fmtNum(live.engine_hours, 0)} h</span>}
        </div>
        <div className="chart">
          <ResponsiveContainer width="100%" height={160}>
              <BarChart data={timeline}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                <XAxis dataKey="label" stroke="var(--muted)" />
                <YAxis yAxisId="l" stroke="var(--muted)" />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', color: 'var(--text)' }} />
                <Legend />
                <Bar yAxisId="l" dataKey="moving_hours" stackId="a" fill="var(--green)" name="Moving (hrs)" />
                <Bar yAxisId="l" dataKey="idle_hours" stackId="a" fill="var(--orange)" name="Idling (hrs)" />
              </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="stats">
          <span>Day: {timelineDay}</span>
          {[0, 1, 2].map((off) => (
            <button key={off} className={`linkbtn ${timelineDay === dayKey(off) ? 'active' : ''}`} onClick={() => setTimelineDay(dayKey(off))}>
              {dayKey(off)}
            </button>
          ))}
        </div>
      </section>

      <section className="panel tonnage">
        <div className="panel-head">
          <h2>Estimated payload <small>(physics solver, 3 days)</small></h2>
        </div>
        <PayloadBar tonnage={tonnage} />
      </section>

      <section className="panel">
        <LoadEventsPanel
          events={loadEvents?.events}
          curve={loadEvents?.curve}
          dwells={dwells}
          loading={!loadEvents}
          error={loadEvents?.error}
          day={loadEventsDay}
          onDay={setLoadEventsDay}
        />
      </section>

      <section className="panel">
        <TripsPanel
          trips={trips}
          comparison={comparison}
          loading={!trips}
          day={tripsDay}
          onDay={setTripsDay}
        />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Driver behaviour</h2>
        </div>
        <DriverPanel profile={profile} loading={profileLoading} />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Telemetry</h2>
          <div className="range">
            {RANGES.map((r) => (
              <button key={r.label} className={r.hours === rangeHours ? 'active' : ''} onClick={() => setRangeHours(r.hours)}>
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className="chart">
          <ResponsiveContainer width="100%" height={300}>
              <LineChart data={series}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                <XAxis dataKey="time" tickFormatter={(t) => new Date(t).toLocaleTimeString()} stroke="var(--muted)" />
                <YAxis yAxisId="left" stroke="var(--accent)" />
                <YAxis yAxisId="right" orientation="right" stroke="var(--orange)" />
                <Tooltip labelFormatter={(t) => new Date(t).toLocaleString()} contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', color: 'var(--text)' }} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="speed" stroke="var(--accent)" dot={false} strokeWidth={2} name="Speed km/h" />
                <Line yAxisId="right" type="monotone" dataKey="rpm" stroke="var(--orange)" dot={false} strokeWidth={2} name="RPM" />
                <Line yAxisId="left" type="monotone" dataKey="load" stroke="var(--green)" dot={false} strokeWidth={1.5} name="Engine load %" />
                <Line yAxisId="right" type="monotone" dataKey="fuel" stroke="var(--yellow)" dot={false} strokeWidth={1.5} name="Fuel L/h" />
              </LineChart>
          </ResponsiveContainer>
        </div>
        {stats && (
          <div className="stats">
            <span>Avg speed {fmtNum(stats.avgSpeed, 1)} km/h</span>
            <span>Max speed {fmtNum(stats.maxSpeed, 1)} km/h</span>
            <span>Avg load {fmtNum(stats.avgLoad)}%</span>
            <span>Avg RPM {fmtNum(stats.avgRpm)}</span>
            <span>km/L {fmtNum(stats.kml, 2)}</span>
            <span>Idle {fmtNum(stats.idleFrac, 1)}%</span>
          </div>
        )}
      </section>
    </div>
  );
}
