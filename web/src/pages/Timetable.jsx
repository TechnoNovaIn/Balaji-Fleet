import { useState, useEffect, useMemo, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { getJson, fmtTime, fmtNum } from '../api';

const HOURS = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);
const LOAD_LABELS = { loaded: 'Loaded', 'loaded idle': 'Loaded (idle)', 'partial load': 'Partial load', 'no load': 'No load' };
const LOAD_COLORS = { loaded: 'var(--green)', 'loaded idle': 'var(--green)', 'partial load': 'var(--yellow)', 'no load': 'var(--bg-2)' };
const TRIP_BADGE = { loaded: 'loaded', empty: 'empty', partial: 'partial_load', unknown: 'partial_load' };
const TRIP_TYPE = { loaded_leg: 'LOADED', empty_leg: 'EMPTY', shunting: 'SHUNT', unknown: 'TRIP' };
const START_EVENT_TITLE = {
  day: 'day boundary', gap: 'parked stop', dep_loaded: 'departed loaded',
  dep_empty: 'departed empty', to_load: 'arrived to load', to_unload: 'arrived to unload', stream: 'mid-stream',
};
const END_EVENT_TITLE = {
  day: 'day boundary', gap: 'parked stop', to_load: 'arrived at loading point',
  to_unload: 'arrived to unload', dep_loaded: 'departed loaded', dep_empty: 'departed empty', stream: 'mid-stream',
};
const END_EVENT = { to_unload: { label: 'UNLOAD', cls: 'loaded' }, to_load: { label: 'LOAD', cls: 'empty' }, gap: { label: 'STOP', cls: 'gap' }, day: { label: 'END', cls: 'gap' } };
const DWELL_LABEL = { loading: 'LOAD', unloading: 'UNLOAD', stop: 'STOP' };

function hourLabel(h) {
  return `${String(h).padStart(2, '0')}:00`;
}

function todayIST() {
  return new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
}

function TimelineRow({ row, index, onClick }) {
  const hasData = row.has_data !== false;
  const summary = hasData ? `${row.schedule.filter((s) => s === 'loaded').length}h loaded` : 'no telemetry';
  const trips = row.trips || [];
  // dwell windows (loading/unloading/stop) -> per-hour band map
  const dwellMap = {}; // hour -> [dwell]
  for (const d of row.dwells || []) {
    for (let h = d.start_hour; h <= d.end_hour; h++) {
      (dwellMap[h] = dwellMap[h] || []).push(d);
    }
  }
  // build a per-hour list of segment badges to overlay on the bar.
  // A trip only shows an END marker when it had a defined start/reason; a
  // mid-stream split (traffic stop) is not a real trip boundary.
  const hourBadges = {}; // hour -> [{edge, label, payload, cls, title}]
  for (const t of trips) {
    if (t.start_hour != null && t.start_event && t.start_event !== 'stream') {
      (hourBadges[t.start_hour] = hourBadges[t.start_hour] || []).push({
        edge: 'start',
        label: t.avg_payload_t != null ? `${t.avg_payload_t.toFixed(0)}t` : (TRIP_TYPE[t.trip_type] || 'DEP'),
        payload: t.avg_payload_t,
        cls: TRIP_BADGE[t.trip_type === 'loaded_leg' ? 'loaded' : t.trip_type === 'empty_leg' ? 'empty' : 'partial'],
        title: `${TRIP_TYPE[t.trip_type] || t.trip_type} leg · start: ${START_EVENT_TITLE[t.start_event] || t.start_event}`
          + (t.avg_payload_t != null ? ` · ${t.avg_payload_t.toFixed(1)}t` : ''),
      });
    }
    if (t.end_hour != null && t.end_event && t.end_event !== 'stream') {
      const e = END_EVENT[t.end_event] || { label: 'END', cls: 'gap' };
      (hourBadges[t.end_hour] = hourBadges[t.end_hour] || []).push({
        edge: 'end', label: e.label, cls: e.cls,
        title: `end: ${END_EVENT_TITLE[t.end_event] || t.end_event}`,
      });
    }
  }
  return (
    <div className={`timeline-row${hasData ? '' : ' no-data'}`} onClick={onClick} role="button" tabIndex={0}
      onKeyDown={(e) => (e.key === 'Enter' ? onClick() : null)}>
      <div className="tl-head">
        <span className="tl-index">{index + 1}</span>
        <span className="tl-plate">{row.vehicle_no}</span>
        <span className="tl-summary">{summary}</span>
      </div>
      <div className="tl-bar" aria-label={`${row.vehicle_no} 24 hour load schedule`}>
        {row.schedule.map((status, i) => {
          const frac = (row.idle_frac && row.idle_frac[i] > 0) ? row.idle_frac[i] : 0;
          const bg = LOAD_COLORS[status] || LOAD_COLORS['no load'];
          const style = { background: bg };
          if (status === 'loaded' && frac > 0) {
            // smooth yellow(top) -> green(bottom) fade; the yellow band grows
            // with idle fraction, blended over a fixed band so there is no
            // hard boundary line
            const y = Math.round(frac * 100);
            const band = 30;
            const from = Math.max(0, y - band / 2);
            const to = Math.min(100, y + band / 2);
            style.background = `linear-gradient(to bottom, var(--yellow) ${from}%, var(--green) ${to}%)`;
          }
          return (
          <span
            key={HOURS[i]}
            className="tl-seg"
            style={style}
            title={`${hourLabel(i)} — ${LOAD_LABELS[status] || status}${frac > 0 ? ` (${(frac * 100).toFixed(0)}% idle)` : ''}`}
          >
            {(hourBadges[i] || []).map((b, j) => (
              <span
                key={j}
                className={`tl-trip-${b.edge} tl-trip-badge ${b.cls || ''}`}
                title={b.title}
              >
                {b.label}
              </span>
            ))}
            {(dwellMap[i] || []).map((d, j) => (
              <span
                key={`d${j}`}
                className={`tl-dwell tl-dwell-${d.type}`}
                title={`${DWELL_LABEL[d.type] || d.type} ${fmtTime(d.start)} → ${fmtTime(d.end)} (${Math.round(d.duration_min)} min) — ${d.why || ''}`}
              >
                {DWELL_LABEL[d.type] || d.type}
              </span>
            ))}
          </span>
          );
        })}
      </div>
    </div>
  );
}

function LiveReadings({ rows, day, onDayChange, onRowClick, onClose }) {
  return (
    <div className="live-overlay" onClick={onClose}>
      <div className="live-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="live-sheet-head">
          <h2>Live readings</h2>
          <div className="live-sheet-controls">
            <input
              type="date"
              className="date-pill"
              value={day}
              max={new Date().toISOString().slice(0, 10)}
              onChange={(e) => onDayChange(e.target.value)}
            />
            <button className="btn btn-sm btn-outline" onClick={onClose}>Close</button>
          </div>
        </div>
        <div className="table-wrap">
          <table className="live-table">
            <thead>
              <tr>
                <th>Vehicle</th>
                <th>Speed</th>
                <th>RPM</th>
                <th>Load</th>
                <th>Accel</th>
                <th>Fuel</th>
                <th>Gear</th>
                <th>Payload</th>
                <th>Last OBD</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.vehicle_no} className="live-row" onClick={() => onRowClick(r)}>
                  <td className="live-plate">
                    <span className="plate">{r.vehicle_no}</span>
                    <span className={`vcard-status ${r.stopped ? 'stopped' : 'live'}`}>{r.stopped ? 'Stopped' : 'Live'}</span>
                  </td>
                  <td>{fmtNum(r.speed, 1)}<small> km/h</small></td>
                  <td>{fmtNum(r.rpm, 0)}</td>
                  <td>{fmtNum(r.engine_load, 0)}<small> %</small></td>
                  <td>{fmtNum(r.accel_pedal, 0)}<small> %</small></td>
                  <td>{fmtNum(r.fuel_rate, 1)}<small> L/h</small></td>
                  <td>{fmtNum(r.gear, 0)}</td>
                  <td>
                    {r.payload_tonnes != null
                      ? <span className="payload-chip set">{fmtNum(r.payload_tonnes, 1)} t</span>
                      : <span className="muted">—</span>}
                  </td>
                  <td className="live-time">{fmtTime(r.obd_time || r.time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <div className="empty">No readings for this day.</div>}
        </div>
      </div>
    </div>
  );
}

export default function Timetable() {
  const navigate = useNavigate();
  const [vehicles, setVehicles] = useState([]);   // schedule data from /timetable
  const [plateRows, setPlateRows] = useState([]); // full fleet rows (present immediately)
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('');
  const [selectedDate, setSelectedDate] = useState(() => todayIST());
  const [showLive, setShowLive] = useState(false);
  const [liveDay, setLiveDay] = useState(() => todayIST());
  const [live, setLive] = useState([]);

  // All fleet rows are rendered from the start (only their cells depend on the
  // schedule fetch). This fetch is fast and gives us the full vehicle list.
  useEffect(() => {
    getJson('/vehicles').then((data) => {
      const list = Array.isArray(data) ? data : data.rows || [];
      setPlateRows(list.map((v) => ({ vehicle_no: v.vehicle_no, payload_tonnes: v.payload_tonnes })));
    }).catch(() => {});
  }, []);

  const fetchSchedule = useCallback((day) => {
    setLoading(true);
    getJson(`/timetable?day=${day}`).then((data) => {
      const rows = Array.isArray(data) ? data : data.rows || [];
      setVehicles(rows);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

   useEffect(() => {
     fetchSchedule(selectedDate);
   }, [selectedDate, fetchSchedule]);

  // When viewing today, auto-refresh the schedule every 60s so newly-computed
  // trip markers (from trips_loop) and fresh telemetry appear automatically.
  useEffect(() => {
     if (selectedDate === todayIST()) {
       const id = setInterval(() => {
         fetchSchedule(selectedDate);
       }, 60000);
       return () => clearInterval(id);
     }
  }, [selectedDate, fetchSchedule]);


  const fetchLive = useCallback((day) => {
    if (!day) return;
    getJson(`/live/day?day=${day}`).then((data) => {
      const rows = Array.isArray(data) ? data : data.rows || [];
      setLive(rows);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (showLive) {
      fetchLive(liveDay);
      const id = setInterval(() => {
        if (liveDay === todayIST()) fetchLive(liveDay);
      }, 15000);
      return () => clearInterval(id);
    }
  }, [showLive, liveDay, fetchLive]);

  // Merge: every fleet row is always present; the schedule fills in as the
  // /timetable response lands.
  const allRows = useMemo(() => {
    if (!plateRows.length) return vehicles;
    const byNo = new Map(vehicles.map((v) => [v.vehicle_no, v]));
    return plateRows.map((p) => byNo.get(p.vehicle_no) || p);
  }, [vehicles, plateRows]);

  const filtered = useMemo(() => {
    if (!filter.trim()) return allRows;
    return allRows.filter((v) => v.vehicle_no.toLowerCase().includes(filter.toLowerCase()));
  }, [allRows, filter]);

  const withDataCount = useMemo(
    () => allRows.filter((v) => v.has_data !== false).length,
    [allRows],
  );

  return (
    <div className="page timetable-page">
      <div className="page-head">
        <div className="page-title-section">
          <h1>24-hour load schedule</h1>
          <p>Per-vehicle load status by hour · tap a vehicle for details</p>
        </div>
        <div className="timetable-toolbar">
          <div className="search-pill-wrapper">
            <span className="search-icon">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" />
              </svg>
            </span>
            <input
              type="text"
              placeholder="Search vehicle…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="search-pill"
            />
          </div>
          <input
            type="date"
            className="date-pill"
            value={selectedDate}
            max={todayIST()}
            onChange={(e) => e.target.value && setSelectedDate(e.target.value)}
          />
          <button className="btn btn-primary" onClick={() => { setLiveDay(todayIST()); setShowLive(true); }}>
            Live readings
          </button>
          <div className="timeline-legend">
            <span className="legend-item"><span className="legend-dot" style={{ background: LOAD_COLORS.loaded }} /> Loaded</span>
            <span className="legend-item"><span className="legend-dot" style={{ background: LOAD_COLORS.loaded, backgroundImage: 'repeating-linear-gradient(45deg, rgba(255,255,255,0.3), rgba(255,255,255,0.3) 2px, transparent 2px, transparent 4px)' }} /> Loaded (idle)</span>
            <span className="legend-item"><span className="legend-dot" style={{ background: LOAD_COLORS['partial load'] }} /> Partial load</span>
            <span className="legend-item"><span className="legend-dot" style={{ background: LOAD_COLORS['no load'] }} /> No load</span>
            <span className="legend-item"><span className="legend-dot" style={{ background: 'transparent', border: '1px dashed var(--border)' }} /> No telemetry</span>
          </div>
        </div>
      </div>

      {loading && (
        <div className="timetable-loader" role="status">
          <span className="spinner" />
          <span>Fetching schedule data for {allRows.length || '…'} vehicles…</span>
        </div>
      )}

      <div className="timeline-board">
        <div className="timeline-ruler">
          <div className="timeline-ruler-spacer">
            <span className="ruler-date">{selectedDate}</span>
            {!loading && allRows.length > 0 && (
              <span className="ruler-meta">{withDataCount} / {allRows.length} vehicles have data</span>
            )}
          </div>
          <div className="timeline-ruler-hours">
            {HOURS.map((h) => (
              <span key={h} className="ruler-tick">{h}</span>
            ))}
          </div>
        </div>

        <div className="timeline-rows">
          {filtered.length === 0 ? (
            <div className="timeline-empty">
              {filter ? `No vehicles match “${filter}”.` : 'Loading fleet…'}
            </div>
          ) : loading ? (
            // full fleet rows are already on screen; bars shimmer until data lands
            filtered.map((v, i) => (
              <div className="timeline-row skeleton-row" key={v.vehicle_no}>
                <div className="tl-head">
                  <span className="tl-index">{i + 1}</span>
                  <span className="tl-plate">{v.vehicle_no}</span>
                </div>
                <div className="sk" style={{ height: 30, borderRadius: 999 }} />
              </div>
            ))
          ) : (
            filtered.map((v, i) => (
              <TimelineRow key={v.vehicle_no} row={v} index={i} onClick={() => navigate(`/vehicle/${v.vehicle_no}`)} />
            ))
          )}
        </div>
      </div>

      {showLive && (
        <LiveReadings
          rows={live}
          day={liveDay}
          onDayChange={setLiveDay}
          onRowClick={(r) => { setShowLive(false); navigate(`/vehicle/${r.vehicle_no}`); }}
          onClose={() => setShowLive(false)}
        />
      )}
    </div>
  );
}
