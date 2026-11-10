import { useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../AuthContext';
import { useTheme } from '../ThemeContext';

function initials(name) {
  return (name || '?').slice(0, 2).toUpperCase();
}

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/vehicles/timetable', label: 'Vehicles', end: false },
  { to: '/maps', label: 'Maps', end: true },
];

export default function Topbar() {
  const { user, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const [open, setOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e) {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    }
    function onKey(e) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [user]);

  return (
    <header className="topbar">
      <div className="topbar-container">
        <div className="topbar-left">
          <button
            className="nav-burger"
            aria-label="Toggle navigation"
            aria-expanded={mobileNavOpen}
            onClick={() => setMobileNavOpen((v) => !v)}
          >
            <span />
            <span />
            <span />
          </button>

          <div className="brand">
            <span className="brand-mark">BF</span>
            <span className="brand-name">
              Balaji <span className="accent">Fleet</span>
            </span>
          </div>
        </div>

        <nav className="nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="topbar-right">
          <button className="theme-toggle" onClick={toggle} aria-label="Toggle color theme" title="Toggle theme">
            {theme === 'dark' ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <circle cx="12" cy="12" r="4.5" />
                <path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M20.7 14.9a8.6 8.6 0 0 1-10.6-10.6 1 1 0 0 0-1.3-1.2A10 10 0 1 0 22 16.1a1 1 0 0 0-1.3-1.2Z" />
              </svg>
            )}
          </button>

          <div className="profile" ref={boxRef}>
            <button className="profile-chip" onClick={() => setOpen((v) => !v)} aria-label="Account menu" aria-expanded={open}>
              <span className="bubble">{initials(user?.username)}</span>
              <span className="profile-name">{user?.username || 'Account'}</span>
              <svg className="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            {open && (
              <div className="menu">
                <div className="menu-user">
                  <span className="bubble bubble-lg">{initials(user?.username)}</span>
                  <div className="menu-user-info">
                    <b>{user?.username}</b>
                    <span>{user?.role === 'admin' ? 'Administrator' : 'Viewer'}</span>
                  </div>
                </div>
                <div className="menu-divider" />
                <button className="menu-item danger" onClick={logout}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                    <path d="M16 17l5-5-5-5" />
                    <path d="M21 12H9" />
                  </svg>
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {mobileNavOpen && (
        <nav className="nav-mobile">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
              onClick={() => setMobileNavOpen(false)}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      )}
    </header>
  );
}
