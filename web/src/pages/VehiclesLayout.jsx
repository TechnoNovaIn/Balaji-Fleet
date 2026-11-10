import { Outlet, NavLink, useLocation } from 'react-router-dom';

const tabs = [
  { to: '/vehicles/timetable', label: 'Timetable' },
  { to: '/vehicles/observation', label: 'Observation' },
  { to: '/vehicles/all', label: 'All Vehicles' },
];

export default function VehiclesLayout() {
  const location = useLocation();
  const isIndividual = location.pathname.startsWith('/vehicle/') || location.pathname === '/vehicle';

  return (
    <div className="vehicles-layout">
      {!isIndividual && (
        <div className="vehicles-nav">
          {tabs.map(t => (
            <NavLink
              key={t.to}
              to={t.to}
              end
              className={({ isActive }) => `nav-tab ${isActive ? 'active' : ''}`}
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      )}
      <div className="vehicles-outlet">
        <Outlet />
      </div>
    </div>
  );
}