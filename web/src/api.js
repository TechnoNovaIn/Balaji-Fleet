const BASE = import.meta.env.VITE_API_URL || '';

function authHeader() {
  const token = localStorage.getItem('balaji_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function handleUnauthorized() {
  localStorage.removeItem('balaji_token');
  localStorage.removeItem('balaji_user');
  window.location.hash = '#/login';
}

export async function getJson(path) {
  const res = await fetch(`${BASE}${path}`, { headers: authHeader() });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error(`${path}: 401 (session expired — signed out)`);
  }
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function postJson(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith('/auth/')) {
    handleUnauthorized();
    throw new Error(`${path}: 401 (session expired — signed out)`);
  }
  if (!res.ok) throw new Error(data.detail || `${path}: ${res.status}`);
  return data;
}

export async function deleteJson(path) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'DELETE',
    headers: authHeader(),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith('/auth/')) {
    handleUnauthorized();
    throw new Error(`${path}: 401 (session expired — signed out)`);
  }
  if (!res.ok) throw new Error(data.detail || `${path}: ${res.status}`);
  return data;
}

export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}
