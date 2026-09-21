"""Application configuration.

Field names are frozen by docs/MODULE_CONTRACT.md §5.  Adding a field is fine;
renaming one is not.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- core ---------------------------------------------------------------
    app_env: str = "dev"
    database_url: str = "sqlite:///./booking.db"
    secret_key: str = "dev-only-change-me"
    public_base_url: str = "http://localhost:8000"
    default_timezone: str = "Asia/Shanghai"

    # -- calendar -----------------------------------------------------------
    calendar_gateway: str = "fake"  # fake | relay | google
    calendar_relay_url: str = ""
    calendar_relay_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    # -- payments -----------------------------------------------------------
    payment_gateway: str = "fake"  # fake | wechat
    wechat_app_id: str = ""
    wechat_mch_id: str = ""
    wechat_api_v3_key: str = ""
    wechat_cert_serial_no: str = ""
    wechat_private_key_path: str = ""
    wechat_public_key_id: str = ""
    wechat_public_key_path: str = ""

    # -- booking policy -----------------------------------------------------
    hold_minutes: int = 10
    slot_step_minutes: int = 15
    sweeper_interval_seconds: int = 60

    # -- rate limiting ------------------------------------------------------
    # BUILD_PLAN §10.  Counters are per process, so the effective budget is these
    # values times the number of uvicorn workers / replicas (see app/rate_limit.py).
    # A human books at most a handful of times an hour, so these are generous.
    rate_limit_bookings_per_hour: int = 20
    rate_limit_slots_per_hour: int = 300
    # Number of trusted reverse proxies in front of the app.  0 means "use the peer
    # address".  Set to 1 behind a single nginx / platform proxy, otherwise the
    # limiter cannot see past the proxy and every caller shares one budget.
    trusted_proxy_depth: int = 0

    # -- email --------------------------------------------------------------
    email_backend: str = "console"  # console | smtp
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""

    # -- celery (prod only) -------------------------------------------------
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def wechat_notify_url(self) -> str:
        """Derived, never configured directly (contract §5)."""
        return f"{self.public_base_url.rstrip('/')}/api/payments/wechat/notify"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
