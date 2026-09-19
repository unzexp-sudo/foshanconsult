"""Calendar relay — the five endpoints of contract §9.

Standalone deployable (contract §2/§14.4): it imports nothing from ``app.*``.  The
booking app, which runs where ``googleapis.com`` is unreachable, calls these
endpoints over HTTPS with an HMAC-signed body.

Behavioural rule that matters most: ``DELETE /events/{event_id}`` is idempotent.
A missing event is reported as ``{"deleted": true}`` — never an error — because
release is retried.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from relay.auth import require_relay_auth
from relay.config import Settings, get_settings
from relay.google import (
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleEventNotFound,
)
from relay.schemas import (
    CreateEventRequest,
    FreeBusyRequest,
    PatchEventRequest,
)


def get_google_client(
    settings: Settings = Depends(get_settings),
) -> GoogleCalendarClient:
    """Build the real Google client from settings (lazy: no network here)."""
    return GoogleCalendarClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        refresh_token=settings.google_refresh_token,
        calendar_id=settings.google_calendar_id,
    )


def _parse(model: type[BaseModel], body: bytes) -> BaseModel:
    """Validate the raw signed bytes.  Malformed JSON is a 400."""
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid request body: {exc}") from exc


def _upstream_error(exc: GoogleCalendarError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def create_app(google_client: GoogleCalendarClient | None = None) -> FastAPI:
    """Build the relay app.

    ``google_client`` injects a fake for tests; production leaves it ``None`` and
    the ``get_google_client`` dependency builds the real one from settings.
    """
    app = FastAPI(title="Calendar Relay", version="1.0.0")

    if google_client is not None:
        app.dependency_overrides[get_google_client] = lambda: google_client

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/freebusy")
    def freebusy(
        body: bytes = Depends(require_relay_auth),
        client: GoogleCalendarClient = Depends(get_google_client),
    ) -> dict:
        request: FreeBusyRequest = _parse(FreeBusyRequest, body)
        try:
            busy = client.freebusy(request.time_min, request.time_max)
        except GoogleCalendarError as exc:
            raise _upstream_error(exc) from exc
        return {"busy": busy}

    @app.post("/events")
    def create_event(
        body: bytes = Depends(require_relay_auth),
        client: GoogleCalendarClient = Depends(get_google_client),
    ) -> dict:
        request: CreateEventRequest = _parse(CreateEventRequest, body)
        try:
            created = client.insert_event(
                summary=request.summary,
                description=request.description,
                start=request.start,
                end=request.end,
                reference=request.reference,
                transparent=request.transparent,
            )
        except GoogleCalendarError as exc:
            raise _upstream_error(exc) from exc
        return {"event_id": created.event_id, "html_link": created.html_link}

    @app.patch("/events/{event_id}")
    def patch_event(
        event_id: str,
        body: bytes = Depends(require_relay_auth),
        client: GoogleCalendarClient = Depends(get_google_client),
    ) -> dict:
        request: PatchEventRequest = _parse(PatchEventRequest, body)
        try:
            client.patch_event(event_id, request.summary, request.description)
        except GoogleEventNotFound:
            # Already gone: not an error for the caller (contract §9).
            pass
        except GoogleCalendarError as exc:
            raise _upstream_error(exc) from exc
        return {"event_id": event_id}

    @app.delete("/events/{event_id}")
    def delete_event(
        event_id: str,
        body: bytes = Depends(require_relay_auth),
        client: GoogleCalendarClient = Depends(get_google_client),
    ) -> dict:
        try:
            client.delete_event(event_id)
        except GoogleEventNotFound:
            # Idempotent release: a missing event is a success (contract §9).
            pass
        except GoogleCalendarError as exc:
            raise _upstream_error(exc) from exc
        return {"deleted": True}

    return app


app = create_app()
