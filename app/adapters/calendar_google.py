"""Direct-to-Google calendar gateway — **local dev only**.

In production the relay (``relay/``) holds the Google credential and the booking
app talks to it over HTTP; this adapter exists so a developer can point the app
at their own calendar without deploying the relay.

The service object is built lazily, so importing this module (and constructing
the gateway) never touches credentials.  ``app.deps`` builds it with no
arguments; tests inject a fake service object.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import settings
from app.ports.calendar import BusyInterval, CalendarEvent, CalendarGatewayError

TOKEN_URI = "https://oauth2.googleapis.com/token"
# A hold is tentative in intent but must block time, so it is opaque.
HOLD_TRANSPARENCY = "opaque"
RELEASED_STATUSES = (404, 410)  # gone / never existed — both mean "already released"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise CalendarGatewayError("naive datetime passed to google calendar gateway")
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CalendarGatewayError(f"google busy interval has non-string {field!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CalendarGatewayError(f"google sent unparseable {field!r}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CalendarGatewayError(f"google sent offset-less {field!r}: {value!r}")
    return parsed.astimezone(UTC)


def _is_http_error(exc: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover - dependency is declared in pyproject
        return False
    return isinstance(exc, HttpError)


def _status_of(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


class GoogleCalendarGateway:
    """``google-api-python-client`` implementation of the calendar port."""

    def __init__(
        self,
        service: object | None = None,
        *,
        calendar_id: str | None = None,
        credentials: object | None = None,
    ) -> None:
        self._service = service
        self._calendar_id = (
            calendar_id if calendar_id is not None else settings.google_calendar_id
        )
        self._credentials = credentials

    # -- lazy service -------------------------------------------------------

    def _get_service(self) -> object:
        if self._service is not None:
            return self._service
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = self._credentials or Credentials(
            token=None,
            refresh_token=settings.google_refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            token_uri=TOKEN_URI,
        )
        self._service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )
        return self._service

    # -- CalendarGateway ----------------------------------------------------

    def freebusy(self, time_min: datetime, time_max: datetime) -> list[BusyInterval]:
        service = self._get_service()
        try:
            result = (
                service.freebusy()  # type: ignore[attr-defined]
                .query(
                    body={
                        "timeMin": _iso(time_min),
                        "timeMax": _iso(time_max),
                        "items": [{"id": self._calendar_id}],
                    }
                )
                .execute()
            )
        except CalendarGatewayError:
            raise
        except Exception as exc:
            raise CalendarGatewayError(f"google freebusy failed: {exc}") from exc

        calendars = result.get("calendars") or {}
        entry = calendars.get(self._calendar_id) or {}
        busy = entry.get("busy") or []
        return [
            BusyInterval(
                start=_parse_utc(item["start"], "start"),
                end=_parse_utc(item["end"], "end"),
            )
            for item in busy
        ]

    def create_hold(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        reference: str,
    ) -> CalendarEvent:
        service = self._get_service()
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": _iso(start)},
            "end": {"dateTime": _iso(end)},
            "transparency": HOLD_TRANSPARENCY,
            "status": "tentative",
            "extendedProperties": {"private": {"reference": reference}},
        }
        try:
            created = (
                service.events()  # type: ignore[attr-defined]
                .insert(calendarId=self._calendar_id, body=body)
                .execute()
            )
        except Exception as exc:
            raise CalendarGatewayError(f"google create_hold failed: {exc}") from exc
        return CalendarEvent(
            event_id=created["id"],
            html_link=created.get("htmlLink"),
        )

    def confirm(self, event_id: str, *, summary: str, description: str) -> None:
        service = self._get_service()
        try:
            (
                service.events()  # type: ignore[attr-defined]
                .patch(
                    calendarId=self._calendar_id,
                    eventId=event_id,
                    body={
                        "summary": summary,
                        "description": description,
                        "status": "confirmed",
                        "transparency": HOLD_TRANSPARENCY,
                    },
                )
                .execute()
            )
        except Exception as exc:
            raise CalendarGatewayError(f"google confirm failed: {exc}") from exc

    def release(self, event_id: str) -> None:
        service = self._get_service()
        try:
            (
                service.events()  # type: ignore[attr-defined]
                .delete(calendarId=self._calendar_id, eventId=event_id)
                .execute()
            )
        except Exception as exc:
            if _is_http_error(exc) and _status_of(exc) in RELEASED_STATUSES:
                return  # already released — idempotent by design
            raise CalendarGatewayError(f"google release failed: {exc}") from exc
