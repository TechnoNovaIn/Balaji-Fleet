import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts';
import { getJson, fmtNum } from '../api';

function Kpi({ label, value, sub, tone }) {
  return (
    <div className={`kpi ${tone || ''}`}>
      <span className="label">{label}</span>
      <b>{value}</b>
      {sub && <small>{sub}</small>}
    </div>
  );
}

export default function Dashboard() {
  const [rows, setRows] = useState([]);
  const [activity, setActivity] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [live, act] = await Promise.all([
          getJson('/live?minutes=20'),
          getJson('/telemetry/fleet-activity'),
        ]);
        if (!cancelled) { setRows(live); setActivity(act); }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    load();
    const id = setInterval(load, 10000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const live = rows.filter((r) => !r.stopped);
  const moving = live.filter((r) => r.speed > 1);
  const idle = live.filter((r) => r.speed <= 1 && r.rpm > 0);
  const stoppedRows = rows.filter((r) => r.stopped);
  const stationaryGps = stoppedRows.filter((r) => r.gps_stationary_20m).length;

  const avg = (arr, k) => (arr.length ? arr.reduce((s, r) => s + (r[k] || 0), 0) / arr.length : 0);
  const loadCond = { Loaded: 0, Empty: 0, Unknown: 0 };
  live.forEach((r) => {
    const c = String(r.vehicle_load_cond || '').toLowerCase();
    if (c.includes('load') || c.includes('filled')) loadCond.Loaded += 1;
    else if (c.includes('empty') || c.includes('unload')) loadCond.Empty += 1;
    else loadCond.Unknown += 1;
  });
  const loadCondData = Object.entries(loadCond).map(([name, n]) => ({ name, n }));

  return (
    <div className="page">
      <div className="page-head">
        <h2>Fleet overview</h2>
        <span className="muted">last 20 min · refreshes every 60s</span>
      </div>

      {error && <div className="banner">API unreachable ({error})</div>}

      <section className="kpi-grid">
        <Kpi label="Fleet size" value={rows.length} sub="registered vehicles" />
        <Kpi label="Live now" value={live.length} sub={`${rows.length ? Math.round((live.length / rows.length) * 100) : 0}% of fleet active`} tone="good" />
        <Kpi label="Moving" value={moving.length} sub={`avg ${fmtNum(avg(moving, 'speed'), 1)} km/h`} tone="good" />
        <Kpi label="Idle" value={idle.length} sub="running but stationary" tone="warn" />
        <Kpi label="Stopped" value={stoppedRows.length} sub={`${stationaryGps} with stationary GPS`} tone="bad" />
      </section>

      <section className="perf-grid">
        <Kpi label="Avg engine load" value={`${fmtNum(avg(live, 'engine_load'))}%`} sub="across live fleet" />
        <Kpi label="Avg fuel rate" value={`${fmtNum(avg(moving, 'fuel_rate'), 1)} L/h`} sub="across moving fleet" />
        <Kpi label="Avg RPM" value={fmtNum(avg(live, 'rpm'))} sub="across live fleet" />
        <Kpi label="Fuel burn today" value={`${fmtNum((moving.length * avg(moving, 'fuel_rate') * 8) / 1000, 1)} kL`} sub="rough fleet estimate" />
      </section>

      <div className="panels">
        <section className="panel">
          <div className="panel-head">
            <h2>Load condition — live fleet</h2>
            <Link to="/vehicles" className="link">View vehicles →</Link>
          </div>
          <div className="chart" style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={loadCondData}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                <XAxis dataKey="name" stroke="var(--muted)" />
                <YAxis stroke="var(--muted)" allowDecimals={false} />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', color: 'var(--text)' }} />
                <Bar dataKey="n" name="vehicles" fill="var(--accent)" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Fleet activity today</h2>
            <span className="muted">moving vs idling per hour</span>
          </div>
          <div className="chart" style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={activity}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                <XAxis dataKey="hour" stroke="var(--muted)" />
                <YAxis stroke="var(--muted)" allowDecimals={false} />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', color: 'var(--text)' }} />
                <Bar dataKey="moving" name="moving" fill="var(--green)" radius={[4, 4, 0, 0]} />
                <Bar dataKey="idling" name="idling" fill="var(--orange)" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>
    </div>
  );
}
