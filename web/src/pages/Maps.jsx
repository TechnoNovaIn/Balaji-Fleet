import { useState, useEffect, useRef } from 'react';
import { getJson } from '../api';
import 'leaflet/dist/leaflet.css';
import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet';
import L from 'leaflet';

const liveIcon = L.divIcon({
  className: 'vehicle-marker-icon',
  html: '<div class="marker-dot live"></div>',
  iconSize: [22, 22],
  iconAnchor: [11, 22],
  popupAnchor: [0, -24],
});

const stoppedIcon = L.divIcon({
  className: 'vehicle-marker-icon',
  html: '<div class="marker-dot stopped"></div>',
  iconSize: [22, 22],
  iconAnchor: [11, 22],
  popupAnchor: [0, -24],
});

export default function MapsPage() {
  const [vehicles, setVehicles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [hovered, setHovered] = useState(null);
  const mapRef = useRef();

  useEffect(() => {
    getJson('/live').then(data => {
      const rows = Array.isArray(data) ? data : data.rows || [];
      const withGps = rows.filter(v => v.lat != null && v.lon != null);
      setVehicles(withGps);
      setLoading(false);

      if (withGps.length > 0 && mapRef.current) {
        const group = L.featureGroup(withGps.map(v =>
          L.marker([v.lat, v.lon]).bindPopup('')
        ));
        mapRef.current.fitBounds(group.getBounds().pad(0.2));
      }
    }).catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="page maps-page">
        <div className="page-head">
          <h1>Fleet Map</h1>
          <p>Loading vehicle positions…</p>
        </div>
        <div className="skeleton" style={{ height: 500, borderRadius: 12 }} />
      </div>
    );
  }

  const center = [12.97, 77.59];

  return (
    <div className="page maps-page">
      <div className="page-head">
        <h1>Fleet Map</h1>
        <p>{vehicles.length} vehicles with GPS data</p>
      </div>

      <div className="map-container">
        <MapContainer ref={mapRef} center={center} zoom={6} style={{ height: 'calc(100vh - 200px)', borderRadius: 12 }}>
          <TileLayer
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          />
          {vehicles.map(v => {
            const isLive = !v.stopped;
            const icon = isLive ? liveIcon : stoppedIcon;
            return (
              <Marker
                key={v.vehicle_no}
                position={[v.lat, v.lon]}
                icon={icon}
                eventHandlers={{
                  mouseover: () => setHovered(v.vehicle_no),
                  mouseout: () => setHovered(null),
                }}
              >
                <Popup className="vehicle-popup">
                  <div style={{ minWidth: 140 }}>
                    <strong style={{ fontSize: 14 }}>{v.vehicle_no}</strong>
                    <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                      {v.vehicle_type || 'Unknown'}
                    </div>
                    <div style={{ fontSize: 11, marginTop: 6, color: v.stopped ? '#f59e0b' : '#22c55e' }}>
                      {v.stopped ? 'Stopped' : 'Live'}
                    </div>
                  </div>
                </Popup>
              </Marker>
            );
          })}
        </MapContainer>

        {hovered && (
          <div className="map-hover-badge">
            {hovered}
          </div>
        )}
      </div>
    </div>
  );
}
