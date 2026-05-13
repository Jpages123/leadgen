"""Application configuration — loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Server ───────────────────────────────────────────────────────
    compose_project_name: str = "cc-leadgen"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ── Database ─────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:postgres@localhost:5432/leadgen"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # ── Redis / Celery ───────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # ── Production CRM Sync ──────────────────────────────────────────
    prod_db_url: str = ""
    prod_tailscale_ip: str = ""

    # ── Google Maps ───────────────────────────────────────────────────
    google_maps_api_key: str = ""
    google_maps_daily_budget_usd: float = 10.0
    google_maps_rate_limit: int = 2

    # ── SMTP ──────────────────────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from_name: str = "Client Compass"
    smtp_from_email: str = ""
    outreach_domain: str = ""
    email_daily_limit: int = 50
    email_min_gap_hours: int = 72

    # ── WhatsApp (deferred) ───────────────────────────────────────────
    meta_graph_version: str = "v22.0"
    whatsapp_number: str = "+27740940550"

    # ── Discovery Sources ─────────────────────────────────────────────
    yellsa_max_pages: int = 10
    cylex_max_pages: int = 10

    # ── Scoring Thresholds ────────────────────────────────────────────
    score_outreach_high: int = 60
    score_outreach_medium: int = 40
    score_outreach_low: int = 20

    # ── Discord Alerts ────────────────────────────────────────────────
    discord_webhook_url: str = ""
    discord_alert_on_reply: bool = True
    discord_alert_on_error: bool = True

    # ── Unsubscribe JWT ───────────────────────────────────────────────
    unsubscribe_secret: str = "change-me"

    # ── Cron Schedule ─────────────────────────────────────────────────
    discovery_schedule_hour: int = 8
    outreach_schedule_minute: str = "*/30"
    response_poll_interval_minutes: int = 15

    @property
    def database_url_sync(self) -> str:
        """Sync URL for psycopg2 / sqlalchemy (not asyncpg)."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()