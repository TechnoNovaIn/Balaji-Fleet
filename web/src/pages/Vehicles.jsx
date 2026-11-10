import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getJson, fmtTime, fmtNum } from '../api';

// Line 1 (header): plate · status · payload · last OBD/GPS
// Line 2: driving essentials
const LINE2 = [
  { key: 'speed', label: 'Speed', digits: 1, unit: 'km/h' },
  { key: 'rpm', label: 'RPM', digits: 0 },
  { key: 'engine_load', label: 'Load', digits: 0, unit: '%' },
  { key: 'accel_pedal', label: 'Accel', digits: 0, unit: '%' },
  { key: 'fuel_rate', label: 'Fuel', digits: 1, unit: 'L/h' },
  { key: 'gear', label: 'Gear', digits: 0 },
  { key: 'odometer', label: 'Odometer', digits: 1, unit: 'km' },
  { key: 'engine_hours', label: 'Eng hrs', digits: 1 },
];
// Line 3: temperatures / pressures / misc
const LINE3 = [
  { key: 'coolant_temp', label: 'Coolant', digits: 0, unit: '°C' },
  { key: 'intake_temp', label: 'Intake', digits: 0, unit: '°C' },
  { key: 'boost_pressure', label: 'Boost', digits: 0, unit: 'kPa' },
  { key: 'oil_pressure', label: 'Oil', digits: 0, unit: 'kPa' },
  { key: 'air_inlet_press', label: 'Air inlet', digits: 0, unit: 'kPa' },
  { key: 'egt', label: 'EGT', digits: 0, unit: '°C' },
  { key: 'ambient_temp', label: 'Ambient', digits: 0, unit: '°C' },
  { key: 'battery_voltage', label: 'Battery', digits: 1, unit: 'V' },
  { key: 'total_fuel', label: 'Total fuel', digits: 1, unit: 'L' },
  { key: 'axle_load', label: 'Axle load', digits: 0 },
  { key: 'axle_load_2', label: 'Axle load 2', digits: 0 },
  { key: 'vehicle_load_cond', label: 'Load cond' },
  { key: 'lift_axle', label: 'Lift axle' },
];

function fmtGps(r) {
  if (r.lat === null || r.lat === undefined || r.lon === null || r.lon === undefined) return '—';
  return `${fmtNum(r.lat, 5)}, ${fmtNum(r.lon, 5)}`;
}

function cellVal(r, c) {
  const v = r[c.key];
  if (v === null || v === undefined || v === '') return <span className="muted">—</span>;
  if (typeof v === 'number') {
    return <>{fmtNum(v, c.digits ?? 0)}{c.unit ? <small> {c.unit}</small> : null}</>;
  }
  return v;
}

function Metric({ r, c }) {
  return (
    <div className="vm" title={c.label}>
      <span className="vm-label">{c.label}</span>
      <span className="vm-value">{cellVal(r, c)}</span>
    </div>
  );
}

function FleetCard({ r, onClick }) {
  return (
    <div className="vcard clickable" onClick={onClick}>
      <div className="vcard-head">
        <span className="plate">{r.vehicle_no}</span>
        <span className={`vcard-status ${r.stopped ? 'stopped' : 'live'}`}>{r.stopped ? 'Stopped' : 'Live'}</span>
        <span
          className={`payload-chip ${r.payload_tonnes != null ? 'set' : ''}`}
          title={r.tonnage_note ? `tonnage: ${r.tonnage_note}` : 'payload (cached tonnage)'}
        >
          {r.payload_tonnes != null ? `${fmtNum(r.payload_tonnes, 1)} t` : <span className="muted">—</span>}
        </span>
        {r.vehicle_type ? (
          <span className="vcard-type" title="vehicle type">{r.vehicle_type}</span>
        ) : null}
        <span className="vcard-times">
          <span>OBD {fmtTime(r.obd_time || r.time)}</span>
          <span>GPS {fmtTime(r.gps_time)}</span>
        </span>
      </div>
      <div className="vcard-line">
        {LINE2.map((c) => <Metric key={c.key} r={r} c={c} />)}
      </div>
      <div className="vcard-line">
        {LINE3.map((c) => <Metric key={c.key} r={r} c={c} />)}
        <div className="vm" title="GPS position">
          <span className="vm-label">GPS</span>
          <span className="vm-value">{fmtGps(r)}</span>
        </div>
      </div>
    </div>
  );
}

function SkeletonCard() {
  return (
    <div className="vcard">
      <div className="vcard-head">
        <span className="sk" style={{ width: 120, height: 16 }} />
        <span className="sk" style={{ width: 60, height: 16 }} />
        <span className="sk" style={{ width: 50, height: 16 }} />
      </div>
      {[0, 1].map((ln) => (
        <div className="vcard-line" key={ln}>
          {Array.from({ length: 7 }).map((_, i) => (
            <div className="vm" key={i}><span className="sk" style={{ width: 56 }} /></div>
          ))}
        </div>
      ))}
    </div>
  );
}

export default function Vehicles() {
  const [vehicles, setVehicles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [error, setError] = useState(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const navigate = useNavigate();

  // Load all vehicles completely in one shot
  const loadVehicles = useCallback(async (isInitial = false) => {
    if (isInitial) setLoading(true);
    try {
      const data = await getJson('/live');
      const list = Array.isArray(data) ? data : (data.rows || []);
      setVehicles(list);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      if (isInitial) setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadVehicles(true);
    const interval = setInterval(() => loadVehicles(false), 20000);
    return () => clearInterval(interval);
  }, [loadVehicles]);

  // Scroll to top button visibility
  useEffect(() => {
    function handleScroll() {
      setShowScrollTop(window.scrollY > 400);
    }
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  function scrollToTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // Instant client-side partial search & status filtering
  const filteredVehicles = useMemo(() => {
    let list = vehicles;

    if (status === 'live') {
      list = list.filter((v) => !v.stopped);
    } else if (status === 'stopped') {
      list = list.filter((v) => v.stopped);
    }

    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter((v) => {
        const no = (v.vehicle_no || '').toLowerCase();
        const type = (v.vehicle_type || '').toLowerCase();
        return no.includes(q) || type.includes(q);
      });
    }

    return list;
  }, [vehicles, status, query]);

  // Dynamic counts for tabs
  const totalCount = vehicles.length;
  const liveCount = useMemo(() => vehicles.filter((v) => !v.stopped).length, [vehicles]);
  const stoppedCount = useMemo(() => vehicles.filter((v) => v.stopped).length, [vehicles]);

  return (
    <div className="page">
      <div className="page-head">
        <h2>All Vehicles</h2>
        <div className="vehicles-toolbar">
          <div className="search-pill-wrapper">
            <span className="search-icon">🔍</span>
            <input
              className="search-pill"
              placeholder="Search vehicle number (e.g. 5158)…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {query && (
              <button
                type="button"
                className="search-clear-btn"
                onClick={() => setQuery('')}
                title="Clear search"
              >
                ✕
              </button>
            )}
          </div>
          <div className="filter-group">
            {[
              { id: 'all', label: `All (${totalCount})` },
              { id: 'live', label: `Live (${liveCount})` },
              { id: 'stopped', label: `Stopped (${stoppedCount})` },
            ].map((tab) => (
              <button
                key={tab.id}
                className={status === tab.id ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-outline'}
                onClick={() => setStatus(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <span className="muted" style={{ fontSize: 13 }}>
            {loading
              ? 'Loading full fleet…'
              : query
              ? `Showing ${filteredVehicles.length} of ${totalCount} matching "${query}"`
              : `All ${totalCount} vehicles loaded`}
          </span>
        </div>
      </div>

      {error && <div className="banner">API unreachable — start FastAPI on :8000 ({error})</div>}

      {loading ? (
        <div className="fleet-cards">
          {Array.from({ length: 8 }).map((_, i) => <SkeletonCard key={i} />)}
        </div>
      ) : (
        <>
          <div className="fleet-cards">
            {filteredVehicles.map((r) => (
              <FleetCard
                key={r.vehicle_no}
                r={r}
                onClick={() => navigate(`/vehicle/${encodeURIComponent(r.vehicle_no)}`)}
              />
            ))}
          </div>

          {filteredVehicles.length === 0 && (
            <div className="empty" style={{ textAlign: 'center', padding: '60px 20px' }}>
              <div style={{ fontSize: 36, marginBottom: 8 }}>🔍</div>
              <p style={{ color: 'var(--muted)', fontSize: 15 }}>
                {query
                  ? `No vehicles found matching "${query}"`
                  : 'No vehicles match current filters.'}
              </p>
            </div>
          )}

          {filteredVehicles.length > 0 && (
            <div className="infinite-footer" style={{ marginTop: 24 }}>
              <span className="infinite-badge">
                ✓ All {filteredVehicles.length} vehicles displayed
              </span>
            </div>
          )}
        </>
      )}

      {showScrollTop && (
        <button className="scroll-top-btn" onClick={scrollToTop} title="Scroll to top">
          ↑ Top
        </button>
      )}
    </div>
  );
}
