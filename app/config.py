import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    balaji_env: str = "dev"

    intangles_base_url: str = "https://external.balajitransports.in/api"
    intangles_username: str = "Intangleuser"
    intangles_password: str = ""

    database_url: str = "postgresql://localhost:5432/balaji"

    # Connection pool sizing — tune for production load
    db_pool_min: int = 2
    db_pool_max: int = 20

    ingest_batch_size: int = 5
    live_batch_size: int = 50
    ingest_delay_seconds: float = 0.2
    poll_lookback_days: int = 1

    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 1440

    # CORS: comma-separated allowed origins, or "*" for dev
    cors_origins: str = "http://localhost:5173,http://localhost:8000"

    def validate_jwt_secret(self) -> None:
        """Warn loudly if the JWT secret is the insecure default."""
        if self.jwt_secret == "dev-secret-change-me":
            if self.balaji_env != "dev":
                raise RuntimeError(
                    "JWT_SECRET is set to the insecure default. "
                    "Set a strong random JWT_SECRET in .env or environment before running in production."
                )
            log.warning(
                "JWT_SECRET is the insecure default — acceptable for local dev only. "
                "Set JWT_SECRET in .env for any other environment."
            )

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


settings = Settings()
