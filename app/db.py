"""Database connection management with connection pooling.

Uses psycopg_pool.ConnectionPool for production-grade connection management:
- Reuses connections instead of opening one per request
- Configurable min/max pool size via DB_POOL_MIN / DB_POOL_MAX
- The ``connect()`` function returns a context-managed connection from the pool
  (drop-in replacement for old ``psycopg.connect()`` callers)
"""

import logging

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import BASE_DIR, settings

log = logging.getLogger(__name__)

# ---------- connection pool (lazy-initialized) ----------

_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        log.info(
            "DB pool opened (min=%d, max=%d)", settings.db_pool_min, settings.db_pool_max
        )
    return _pool


def connect():
    """Return a context-managed connection from the pool.

    Usage is identical to the old ``psycopg.connect()`` pattern::

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    return _get_pool().connection()


def close_pool():
    """Drain and close the pool (for clean shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("DB pool closed")


# ---------- schema bootstrap ----------


def schema_ready() -> bool:
    """True if the core hypertables already exist. init_db.sql holds ACCESS
    EXCLUSIVE locks; running it on every startup of an already-initialized DB
    deadlocks/races the live poller's long transactions, so callers should only
    run init_schema() when this returns False."""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT (to_regclass('public.telemetry') IS NOT NULL AND "
                    "to_regclass('public.trips') IS NOT NULL) AS ready"
                )
                return bool(cur.fetchone()["ready"])
    except Exception:
        return False


def init_schema():
    schema_path = BASE_DIR / "scripts" / "init_db.sql"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text())
        conn.commit()
