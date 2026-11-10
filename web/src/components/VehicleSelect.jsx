import { useState, useEffect, useRef } from 'react';
import { getJson } from '../api';

let cachedVehicles = null;

export default function VehicleSelect({ value, onChange, placeholder = 'Vehicle no (e.g. KA06AB1234) *', required = true }) {
  const [vehicles, setVehicles] = useState(cachedVehicles || []);
  const [isOpen, setIsOpen] = useState(false);
  const [highlightIndex, setHighlightIndex] = useState(0);
  const [search, setSearch] = useState(value || '');
  const containerRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    setSearch(value || '');
  }, [value]);

  useEffect(() => {
    if (cachedVehicles) {
      setVehicles(cachedVehicles);
      return;
    }
    let cancelled = false;
    getJson('/vehicles')
      .then((data) => {
        if (!cancelled && Array.isArray(data)) {
          cachedVehicles = data;
          setVehicles(data);
        }
      })
      .catch((e) => console.error('Failed to load vehicles for select:', e));
    return () => {
      cancelled = true;
    };
  }, []);

  // Filter matching vehicles with partial search (plate or type)
  const term = (search || '').trim().toLowerCase();
  const filtered = vehicles.filter((v) => {
    if (!term) return true;
    const no = (v.vehicle_no || '').toLowerCase();
    const type = (v.vehicle_type || '').toLowerCase();
    return no.includes(term) || type.includes(term);
  });

  // Close dropdown on click outside
  useEffect(() => {
    function handleClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Keep highlighted item in view
  useEffect(() => {
    if (isOpen && listRef.current && listRef.current.children[highlightIndex]) {
      listRef.current.children[highlightIndex].scrollIntoView({
        block: 'nearest',
      });
    }
  }, [highlightIndex, isOpen]);

  function handleSelect(plate) {
    onChange(plate);
    setSearch(plate);
    setIsOpen(false);
  }

  function handleInputChange(e) {
    const val = e.target.value.toUpperCase();
    setSearch(val);
    onChange(val);
    setIsOpen(true);
    setHighlightIndex(0);
  }

  function handleKeyDown(e) {
    if (!isOpen) {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        setIsOpen(true);
        e.preventDefault();
      }
      return;
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlightIndex((prev) => (prev + 1 < filtered.length ? prev + 1 : 0));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlightIndex((prev) => (prev - 1 >= 0 ? prev - 1 : filtered.length - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (filtered[highlightIndex]) {
        handleSelect(filtered[highlightIndex].vehicle_no);
      }
    } else if (e.key === 'Escape') {
      setIsOpen(false);
    }
  }

  // Highlight matched substring
  function highlightMatch(text, match) {
    if (!match) return text;
    const index = text.toLowerCase().indexOf(match.toLowerCase());
    if (index === -1) return text;
    const before = text.substring(0, index);
    const middle = text.substring(index, index + match.length);
    const after = text.substring(index + match.length);
    return (
      <>
        {before}
        <strong className="vs-highlight">{middle}</strong>
        {after}
      </>
    );
  }

  return (
    <div className="vehicle-select-container" ref={containerRef}>
      <div className="vehicle-select-input-wrapper">
        <span className="vs-icon">🚚</span>
        <input
          type="text"
          className="vehicle-select-input"
          placeholder={placeholder}
          value={search}
          onChange={handleInputChange}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
          required={required}
          autoComplete="off"
        />
        {search && (
          <button
            type="button"
            className="vs-clear-btn"
            onClick={() => {
              setSearch('');
              onChange('');
              setIsOpen(true);
            }}
            title="Clear vehicle"
          >
            ✕
          </button>
        )}
        <button
          type="button"
          className={`vs-toggle-btn ${isOpen ? 'open' : ''}`}
          onClick={() => setIsOpen(!isOpen)}
          title="Toggle vehicle list"
        >
          ▾
        </button>
      </div>

      {isOpen && (
        <div className="vehicle-select-dropdown">
          <div className="vs-dropdown-header">
            <span>
              {filtered.length} {filtered.length === 1 ? 'vehicle' : 'vehicles'} found
              {term ? ` for "${term}"` : ''}
            </span>
            {filtered.length > 0 && <span className="vs-hint">↑↓ to navigate, ↵ to select</span>}
          </div>

          <div className="vehicle-select-list" ref={listRef}>
            {filtered.length === 0 ? (
              <div className="vs-empty">
                <span>No vehicles match "{term}"</span>
                <small className="muted">You can still use "{term}" as entered</small>
              </div>
            ) : (
              filtered.slice(0, 100).map((v, i) => (
                <div
                  key={v.vehicle_no || i}
                  className={`vs-item ${i === highlightIndex ? 'highlighted' : ''} ${
                    v.vehicle_no === value ? 'selected' : ''
                  }`}
                  onMouseEnter={() => setHighlightIndex(i)}
                  onClick={() => handleSelect(v.vehicle_no)}
                >
                  <div className="vs-item-plate">
                    {highlightMatch(v.vehicle_no, term)}
                  </div>
                  <div className="vs-item-meta">
                    {v.vehicle_type && <span className="vs-badge-type">{v.vehicle_type}</span>}
                    {v.payload_tonnes != null && (
                      <span className="vs-badge-payload">{Number(v.payload_tonnes).toFixed(1)}t</span>
                    )}
                  </div>
                </div>
              ))
            )}
            {filtered.length > 100 && (
              <div className="vs-more-hint">
                + {filtered.length - 100} more matching vehicles (type to narrow down)
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
