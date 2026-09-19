"""Thin Google Calendar client — the only code in the system that talks to Google.

The service object is built lazily from a refresh token, so importing this module
(and building the FastAPI app) never needs credentials and never touches the
network.  Every ``HttpError`` is wrapped in :class:`GoogleCalendarError` carrying
Google's own message.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)


class GoogleCalendarError(Exception):
    """Any failure talking to Google Calendar, carrying Google's message."""


class GoogleEventNotFound(GoogleCalendarError):
    """The referenced event does not exist (Google returned 404)."""


@dataclass(frozen=True)
class InsertedEvent:
    event_id: str
    html_link: str | None = None


def _is_not_found(exc: HttpError) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 404


def _wrap(exc: HttpError, action: str) -> GoogleCalendarError:
    reason = getattr(exc, "reason", None) or str(exc)
    return GoogleCalendarError(f"Google Calendar {action} failed: {reason}")


class GoogleCalendarClient:
    """Minimal Calendar v3 client for the three relay operations."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id
        self._service = None

    @property
    def service(self):
        """The Calendar v3 resource, built on first use."""
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        credentials = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=list(GOOGLE_CALENDAR_SCOPES),
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def freebusy(self, time_min: str, time_max: str) -> list[dict[str, str]]:
        """Busy intervals overlapping ``[time_min, time_max)``."""
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": self.calendar_id}],
        }
        try:
            response = self.service.freebusy().query(body=body).execute()
        except HttpError as exc:
            raise _wrap(exc, "freebusy query") from exc

        calendars = response.get("calendars") or {}
        entry = calendars.get(self.calendar_id) or {}
        busy = entry.get("busy") or []
        return [{"start": slot["start"], "end": slot["end"]} for slot in busy]

    def insert_event(
        self,
        *,
        summary: str,
        description: str,
        start: str,
        end: str,
        reference: str,
        transparent: bool,
    ) -> InsertedEvent:
        """Create an event.  ``transparent=True`` does not block time."""
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
            "transparency": "transparent" if transparent else "opaque",
            "extendedProperties": {"private": {"reference": reference}},
        }
        try:
            created = (
                self.service.events()
                .insert(calendarId=self.calendar_id, body=body)
                .execute()
            )
        except HttpError as exc:
            raise _wrap(exc, "event insert") from exc

        return InsertedEvent(event_id=created["id"], html_link=created.get("htmlLink"))

    def patch_event(self, event_id: str, summary: str, description: str) -> None:
        """Rewrite an existing event's title/description."""
        body = {"summary": summary, "description": description}
        try:
            self.service.events().patch(
                calendarId=self.calendar_id, eventId=event_id, body=body
            ).execute()
        except HttpError as exc:
            if _is_not_found(exc):
                raise GoogleEventNotFound(f"event {event_id!r} not found") from exc
            raise _wrap(exc, "event patch") from exc

    def delete_event(self, event_id: str) -> None:
        """Delete an event.  A 404 is surfaced as :class:`GoogleEventNotFound`."""
        try:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except HttpError as exc:
            if _is_not_found(exc):
                raise GoogleEventNotFound(f"event {event_id!r} not found") from exc
            raise _wrap(exc, "event delete") from exc
