import { useState, useEffect } from 'react';
import { getJson, postJson, deleteJson, fmtTime, fmtNum } from '../api';
import VehicleSelect from '../components/VehicleSelect';

function PatternGraph({ pattern }) {
  const { type, value, peak } = pattern;
  const height = Math.max(8, (value / 100) * 60);
  const pct = Math.round(value);
  return (
    <div className="pattern-graph" title={`${type}: ${pct}% (peak ${Math.round(peak || 0)}%)`}>
      <div className="pg-bar" style={{ height }}>
        <span className="pg-value">{pct}%</span>
      </div>
      <div className="pg-label">{type}</div>
    </div>
  );
}

function AnomalyList({ anomalies }) {
  if (!anomalies?.length) return null;
  return (
    <div className="anomaly-list">
      <div className="anomaly-title">Anomalous driving windows (Isolation Forest)</div>
      {anomalies.map((a, i) => (
        <div key={i} className="anomaly-row">
          <span className="anomaly-time">{fmtTime(a.time)}</span>
          <span>rpm {fmtNum(a.rpm)}</span>
          <span>fuel {fmtNum(a.fuel_rate, 1)} L/h</span>
          <span>speed {fmtNum(a.speed)} km/h</span>
          <span className="anomaly-score">score {a.score}</span>
        </div>
      ))}
    </div>
  );
}

function ProfileData({ p }) {
  if (!p.trained_at) return null;
  const d = p.profile_data || {};
  const sum = d.summary || {};
  const lb = d.load_breakdown || {};
  const fuelPct = d.fleet_fuel_percentile;
  return (
    <div className="profile-data">
      <div className="pd-kpis">
        <div className="pd-kpi"><span className="pd-kpi-v">{sum.windows ?? '—'}</span><span className="pd-kpi-l">windows</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{fmtNum(sum.kmh_avg, 1)}</span><span className="pd-kpi-l">avg km/h</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{sum.styles ?? '—'}</span><span className="pd-kpi-l">driving styles</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{fmtNum(d.km_per_l, 1)}</span><span className="pd-kpi-l">km/l</span></div>
        <div className="pd-kpi"><span className="pd-kpi-v">{fmtNum(d.shift_rate_per_min, 1)}</span><span className="pd-kpi-l">shifts/min</span></div>
      </div>

      {lb.loaded != null && (
        <div className="pd-load">
          <span className="pd-load-label">Load-normalized mix:</span>
          <div className="pd-load-bar">
            <span className="pd-load-seg pd-load-empty" style={{ width: `${(lb.empty || 0) * 100}%` }} title={`empty ${Math.round((lb.empty || 0) * 100)}%`} />
            <span className="pd-load-seg pd-load-loaded" style={{ width: `${(lb.loaded || 0) * 100}%` }} title={`loaded ${Math.round((lb.loaded || 0) * 100)}%`} />
          </div>
          <span className="pd-load-nums">
            {Math.round((lb.empty || 0) * 100)}% empty · {Math.round((lb.loaded || 0) * 100)}% loaded
          </span>
        </div>
      )}

      {fuelPct != null && (
        <div className="pd-fuel">
          <span className="pd-fuel-label">Fuel economy vs fleet:</span>
          <div className="pd-fuel-bar">
            <span className="pd-fuel-fill" style={{ width: `${fuelPct}%` }} />
          </div>
          <span className="pd-fuel-nums">{fuelPct}th percentile ({d.fleet_fuel_samples} samples)</span>
        </div>
      )}

      <AnomalyList anomalies={d.anomalies} />
    </div>
  );
}

export default function Observation() {
  const [profiles, setProfiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(null);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ driver_name: '', vehicle_no: '' });

  useEffect(() => { loadProfiles(); }, []);

  function loadProfiles() {
    setLoading(true);
    getJson('/driver-profiles').then(data => {
      setProfiles(Array.isArray(data) ? data : []);
      setLoading(false);
    }).catch(e => { setError(e.message); setLoading(false); });
  }

  function handleCreate() {
    const driver_name = form.driver_name.trim();
    const vehicle_no = form.vehicle_no.trim().toUpperCase();
    if (!driver_name) {
      setError('Driver name is required');
      return;
    }
    if (!vehicle_no) {
      setError('Vehicle number is required');
      return;
    }
    setError(null);
    postJson('/driver-profiles', {
      driver_name,
      vehicle_no,
      profile_data: {},
      driving_patterns: [],
      route_patterns: [],
    })
      .then(() => {
        setShowForm(false);
        setForm({ driver_name: '', vehicle_no: '' });
        loadProfiles();
      })
      .catch((e) => setError(e.message));
  }

  async function handleDelete(id, name) {
    if (!window.confirm(`Delete driver profile for "${name || 'Unnamed Driver'}"?`)) return;
    setError(null);
    try {
      await deleteJson(`/driver-profiles/${id}`);
      loadProfiles();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleAnalyze(id) {
    setAnalyzing(id);
    setError(null);
    try {
      await postJson(`/driver-profiles/${id}/train`, {});
      loadProfiles();
    } catch (e) {
      setError(e.message);
    } finally {
      setAnalyzing(null);
    }
  }

  return (
    <div className="page observation-page">
      <div className="page-head">
        <div className="page-title-section">
          <h1>Driver Patterns</h1>
          <p>Behavioural fingerprints clustered from telemetry — load-normalized via the tonnage classifier</p>
        </div>
        <button className="btn btn-outline" onClick={() => setShowForm(!showForm)} style={{ alignSelf: 'flex-end' }}>
          {showForm ? '← Back' : '+ Add Driver'}
        </button>
      </div>

      {error && <div className="alert-error">{error}</div>}

      {showForm && (
        <div className="card card-form">
          <div className="card-form-body">
            <input
              placeholder="Driver name *"
              value={form.driver_name}
              onChange={e => setForm({ ...form, driver_name: e.target.value })}
            />
            <VehicleSelect
              placeholder="Search vehicle no (e.g. 5158, KA06)... *"
              value={form.vehicle_no}
              onChange={val => setForm({ ...form, vehicle_no: val })}
            />
            <button className="btn btn-primary" onClick={handleCreate}>Create</button>
          </div>
        </div>
      )}

      <div className="observation-list">
        {loading ? (
          <div className="skeleton" style={{ height: 160 }} />
        ) : profiles.length === 0 ? (
          <div className="observation-empty">
            <div className="observation-empty-icon">📊</div>
            <h3>No driver profiles yet</h3>
            <p>Add a driver + vehicle and click "Analyze" to compute a behavioural fingerprint from the last 7 days of telemetry.</p>
            <button className="btn btn-primary" onClick={() => setShowForm(true)}>
              + Create First Profile
            </button>
          </div>
        ) : (
          profiles.map(p => (
            <div key={p.id} className="card observation-card">
              <div className="observation-card-head">
                <div className="observation-card-title">
                  {p.driver_name || 'Unnamed Driver'}
                  {p.vehicle_no && <span className="observation-vehicle">→ {p.vehicle_no}</span>}
                </div>
                <div className="observation-card-actions">
                  {p.trained_at && (
                    <span className="badge badge-trained">
                      {p.confidence ? `trained · ${Math.round(p.confidence * 100)}% conf` : 'trained · low'}
                    </span>
                  )}
                  <button className="btn btn-sm btn-outline" onClick={() => handleAnalyze(p.id)} disabled={analyzing !== null}>
                    {analyzing === p.id ? 'Analyzing…' : (p.trained_at ? 'Re-analyze' : 'Analyze')}
                  </button>
                  <button className="btn btn-sm btn-danger-outline" onClick={() => handleDelete(p.id, p.driver_name)} title="Delete profile">
                    Delete
                  </button>
                </div>
              </div>

              {p.driving_patterns && p.driving_patterns.length > 0 && (
                <div className="pattern-grid">
                  {p.driving_patterns.map((pat, i) => (
                    <PatternGraph key={i} pattern={pat} />
                  ))}
                </div>
              )}

              {!p.driving_patterns?.length && (
                <div className="muted" style={{ padding: '8px 0', fontSize: 12 }}>
                  {p.trained_at ? 'No pattern data available' : 'Click "Analyze" to compute driving patterns from telemetry'}
                </div>
              )}

              <ProfileData p={p} />

              <div className="observation-meta">
                <span>Created: {p.created_at ? new Date(p.created_at).toLocaleDateString() : '—'}</span>
                <span>Last analyzed: {p.trained_at ? fmtTime(p.trained_at) : '—'}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
