from app.routers.drivers import router as drivers_router
from app.routers.ingest import router as ingest_router
from app.routers.telemetry import router as telemetry_router
from app.routers.vehicles import router as vehicles_router

__all__ = ["drivers_router", "ingest_router", "telemetry_router", "vehicles_router"]
