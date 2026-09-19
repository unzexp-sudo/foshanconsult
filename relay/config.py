"""Relay configuration.

The relay is a separate deployable (contract §14.4): it holds the Google
credentials and never imports anything from ``app.*``.  ``relay_secret`` is the
HMAC secret shared with the booking app's ``RelayCalendarGateway``.
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

    # Shared secret for the HMAC scheme of contract §9.
    relay_secret: str = ""

    # Google OAuth2 refresh-token credentials for the calendar being proxied.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
