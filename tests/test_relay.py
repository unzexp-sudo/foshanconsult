"""Relay tests — no network, no credentials, no ``app.*`` imports.

The Google layer is replaced with a local fake via ``create_app(google_client=...)``
and requests are signed with ``relay.auth.build_signature``, the same helper the
booking app's ``RelayCalendarGateway`` uses.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from relay.auth import build_signature
from relay.config import Settings, get_settings
from relay.google import (
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleEventNotFound,
    InsertedEvent,
)
from relay.main import create_app

SECRET = "test-secret-123"
BUSY = [
    {"start": "2026-09-19T09:00:00+08:00", "end": "2026-09-19T09:30:00+08:00"},
    {"start": "2026-09-19T14:00:00+08:00", "end": "2026-09-19T15:00:00+08:00"},
]


# --------------------------------------------------------------------------- #
# fake Google client
# --------------------------------------------------------------------------- #
class FakeGoogleClient:
    def __init__(self, busy=None):
        self.busy = list(BUSY if busy is None else busy)
        self.events: dict[str, dict[str, str]] = {}
        self.freebusy_calls: list[tuple[str, str]] = []
        self.insert_calls: list[dict] = []
        self.patch_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[str] = []

    def freebusy(self, time_min, time_max):
        self.freebusy_calls.append((time_min, time_max))
        return list(self.busy)

    def insert_event(self, *, summary, description, start, end, reference, transparent):
        self.insert_calls.append(
            {
                "summary": summary,
                "description": description,
                "start": start,
                "end": end,
                "reference": reference,
                "transparent": transparent,
            }
        )
        event_id = f"evt-{len(self.events) + 1}"
        self.events[event_id] = {"summary": summary, "description": description}
        return InsertedEvent(
            event_id=event_id,
            html_link=f"https://calendar.google.com/calendar/event?eid={event_id}",
        )

    def patch_event(self, event_id, summary, description):
        self.patch_calls.append((event_id, summary, description))
        if event_id not in self.events:
            raise GoogleEventNotFound(f"event {event_id!r} not found")
        self.events[event_id] = {"summary": summary, "description": description}

    def delete_event(self, event_id):
        self.delete_calls.append(event_id)
        if event_id not in self.events:
            raise GoogleEventNotFound(f"event {event_id!r} not found")
        del self.events[event_id]


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_google() -> FakeGoogleClient:
    return FakeGoogleClient()


@pytest.fixture
def client(fake_google) -> TestClient:
    app = create_app(google_client=fake_google)
    app.dependency_overrides[get_settings] = lambda: Settings(relay_secret=SECRET)
    with TestClient(app) as test_client:
        yield test_client


def _body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _sign(body: bytes, *, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "X-Relay-Timestamp": str(ts),
        "X-Relay-Signature": build_signature(secret, ts, body),
    }


def _call(client, method, path, payload=None, *, secret=SECRET, timestamp=None, headers=None):
    body = b"" if payload is None else _body(payload)
    request_headers = _sign(body, secret=secret, timestamp=timestamp)
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    return client.request(method, path, content=body, headers=request_headers)


FREE_BUSY_PAYLOAD = {
    "time_min": "2026-09-19T00:00:00+08:00",
    "time_max": "2026-09-20T00:00:00+08:00",
}
EVENT_PAYLOAD = {
    "summary": "HOLD · Ada · BK7Q2M4X",
    "description": "1-1 consultation",
    "start": "2026-09-19T09:00:00+08:00",
    "end": "2026-09-19T09:30:00+08:00",
    "reference": "BK7Q2M4X",
    "transparent": False,
}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_correctly_signed_request_succeeds(client, fake_google):
    response = _call(client, "POST", "/freebusy", FREE_BUSY_PAYLOAD)
    assert response.status_code == 200
    assert fake_google.freebusy_calls == [
        ("2026-09-19T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    ]


def test_wrong_signature_is_rejected(client, fake_google):
    response = _call(client, "POST", "/freebusy", FREE_BUSY_PAYLOAD, secret="wrong-secret")
    assert response.status_code == 401
    assert "detail" in response.json()
    assert fake_google.freebusy_calls == []


def test_stale_timestamp_is_rejected(client):
    response = _call(
        client, "POST", "/freebusy", FREE_BUSY_PAYLOAD, timestamp=int(time.time()) - 301
    )
    assert response.status_code == 401


def test_future_timestamp_is_rejected(client):
    response = _call(
        client, "POST", "/freebusy", FREE_BUSY_PAYLOAD, timestamp=int(time.time()) + 301
    )
    assert response.status_code == 401


def test_timestamp_at_edge_of_window_is_accepted(client):
    response = _call(
        client, "POST", "/freebusy", FREE_BUSY_PAYLOAD, timestamp=int(time.time()) - 299
    )
    assert response.status_code == 200


def test_missing_timestamp_is_rejected(client):
    body = _body(FREE_BUSY_PAYLOAD)
    headers = _sign(body)
    headers.pop("X-Relay-Timestamp")
    response = client.post(
        "/freebusy", content=body, headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 401


def test_missing_signature_is_rejected(client):
    body = _body(FREE_BUSY_PAYLOAD)
    headers = _sign(body)
    headers.pop("X-Relay-Signature")
    response = client.post(
        "/freebusy", content=body, headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 401


def test_mutated_body_is_rejected(client, fake_google):
    signed_body = _body(FREE_BUSY_PAYLOAD)
    headers = _sign(signed_body)
    mutated_body = _body(
        {"time_min": "2026-09-19T00:00:00+08:00", "time_max": "2026-09-21T00:00:00+08:00"}
    )
    response = client.post(
        "/freebusy", content=mutated_body, headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 401
    assert fake_google.freebusy_calls == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/freebusy", FREE_BUSY_PAYLOAD),
        ("POST", "/events", EVENT_PAYLOAD),
        ("PATCH", "/events/evt-1", {"summary": "s", "description": "d"}),
        ("DELETE", "/events/evt-1", None),
    ],
)
def test_every_endpoint_requires_auth(client, method, path, payload):
    body = b"" if payload is None else _body(payload)
    response = client.request(method, path, content=body)
    assert response.status_code == 401
    assert "detail" in response.json()


# --------------------------------------------------------------------------- #
# freebusy
# --------------------------------------------------------------------------- #
def test_freebusy_maps_google_response_with_offsets(client):
    response = _call(client, "POST", "/freebusy", FREE_BUSY_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"busy": BUSY}


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("transparent", [True, False])
def test_create_event_passes_transparency_through(client, fake_google, transparent):
    payload = {**EVENT_PAYLOAD, "transparent": transparent}
    response = _call(client, "POST", "/events", payload)
    assert response.status_code == 200
    assert response.json() == {
        "event_id": "evt-1",
        "html_link": "https://calendar.google.com/calendar/event?eid=evt-1",
    }
    assert fake_google.insert_calls[0]["transparent"] is transparent


def test_patch_event_returns_event_id(client, fake_google):
    _call(client, "POST", "/events", EVENT_PAYLOAD)
    response = _call(
        client, "PATCH", "/events/evt-1", {"summary": "1-1 consultation", "description": "paid"}
    )
    assert response.status_code == 200
    assert response.json() == {"event_id": "evt-1"}
    assert fake_google.patch_calls == [("evt-1", "1-1 consultation", "paid")]


def test_delete_event_returns_deleted_true(client, fake_google):
    _call(client, "POST", "/events", EVENT_PAYLOAD)
    response = _call(client, "DELETE", "/events/evt-1")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert fake_google.events == {}


def test_delete_unknown_event_is_idempotent_success(client, fake_google):
    response = _call(client, "DELETE", "/events/does-not-exist")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert fake_google.delete_calls == ["does-not-exist"]


def test_patch_unknown_event_is_not_an_error(client, fake_google):
    response = _call(
        client, "PATCH", "/events/does-not-exist", {"summary": "s", "description": "d"}
    )
    assert response.status_code == 200
    assert response.json() == {"event_id": "does-not-exist"}


def test_google_failure_becomes_502_with_detail(client, fake_google):
    def boom(event_id):
        raise GoogleCalendarError("Google Calendar event delete failed: quota exceeded")

    fake_google.delete_event = boom
    response = _call(client, "DELETE", "/events/evt-1")
    assert response.status_code == 502
    assert "quota exceeded" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# real Google client mapping (fake service, still no network)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int, reason: str = "Not Found") -> None:
        self.status = status
        self.reason = reason


class _FakeHttpRequest:
    def __init__(self, result=None, error=None) -> None:
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeEventsResource:
    def __init__(self, calls: dict) -> None:
        self._calls = calls

    def insert(self, **kwargs):
        self._calls["insert"] = kwargs
        return _FakeHttpRequest(result={"id": "g-1", "htmlLink": "https://cal/g-1"})

    def patch(self, **kwargs):
        self._calls["patch"] = kwargs
        return _FakeHttpRequest(result={})

    def delete(self, **kwargs):
        self._calls["delete"] = kwargs
        return _FakeHttpRequest(error=self._calls.get("delete_error"))


class _FakeFreeBusyResource:
    def __init__(self, calls: dict) -> None:
        self._calls = calls

    def query(self, **kwargs):
        self._calls["freebusy"] = kwargs
        return _FakeHttpRequest(result=self._calls.get("freebusy_result"))


class _FakeService:
    def __init__(self, calls: dict) -> None:
        self._calls = calls

    def events(self):
        return _FakeEventsResource(self._calls)

    def freebusy(self):
        return _FakeFreeBusyResource(self._calls)


def _google_client(calls: dict) -> GoogleCalendarClient:
    client = GoogleCalendarClient(
        client_id="id", client_secret="secret", refresh_token="refresh", calendar_id="primary"
    )
    client._service = _FakeService(calls)
    return client


@pytest.mark.parametrize(("transparent", "expected"), [(True, "transparent"), (False, "opaque")])
def test_google_insert_sets_transparency_field(transparent, expected):
    calls: dict = {}
    created = _google_client(calls).insert_event(
        summary="HOLD · Ada · BK1",
        description="1-1 consultation",
        start="2026-09-19T09:00:00+08:00",
        end="2026-09-19T09:30:00+08:00",
        reference="BK1",
        transparent=transparent,
    )
    assert created.event_id == "g-1"
    assert created.html_link == "https://cal/g-1"
    assert calls["insert"]["body"]["transparency"] == expected
    assert calls["insert"]["body"]["extendedProperties"]["private"]["reference"] == "BK1"


def test_google_freebusy_extracts_busy():
    calls = {
        "freebusy_result": {
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-09-19T01:00:00Z", "end": "2026-09-19T02:00:00Z"}
                    ]
                }
            }
        }
    }
    busy = _google_client(calls).freebusy("2026-09-19T00:00:00Z", "2026-09-20T00:00:00Z")
    assert busy == [{"start": "2026-09-19T01:00:00Z", "end": "2026-09-19T02:00:00Z"}]


def test_google_delete_404_maps_to_not_found():
    calls = {"delete_error": HttpError(_FakeResponse(404), b'{"error": {"message": "Not Found"}}')}
    with pytest.raises(GoogleEventNotFound):
        _google_client(calls).delete_event("ghost")


def test_google_non_404_error_is_wrapped_with_message():
    calls = {
        "delete_error": HttpError(
            _FakeResponse(500, "Internal Server Error"), b'{"error": {"message": "boom"}}'
        )
    }
    with pytest.raises(GoogleCalendarError) as excinfo:
        _google_client(calls).delete_event("evt-1")
    assert not isinstance(excinfo.value, GoogleEventNotFound)
    assert "boom" in str(excinfo.value)
