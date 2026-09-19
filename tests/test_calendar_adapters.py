"""Tests for the three calendar adapters.  No network, no credentials.

The relay client is driven through ``httpx.MockTransport``; the Google client
through an injected fake service object.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httplib2
import httpx
import pytest
from googleapiclient.errors import HttpError

from app.adapters.calendar_fake import FakeCalendarGateway
from app.adapters.calendar_google import GoogleCalendarGateway
from app.adapters.calendar_relay import RelayCalendarGateway
from app.ports.calendar import (
    BusyInterval,
    CalendarEvent,
    CalendarGateway,
    CalendarGatewayError,
)

SECRET = "relay-test-secret"
BASE_URL = "http://relay.test"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _relay(handler) -> tuple[RelayCalendarGateway, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    return RelayCalendarGateway(base_url=BASE_URL, secret=SECRET, client=client), seen


def _http_error(status: int) -> HttpError:
    response = httplib2.Response({"status": status, "reason": "err"})
    return HttpError(response, b'{"error": {"message": "boom"}}')


# ---------------------------------------------------------------------------
# Protocol conformance + credential-free construction
# ---------------------------------------------------------------------------


def test_all_adapters_satisfy_the_protocol_and_construct_with_no_arguments():
    for gateway in (
        FakeCalendarGateway(),
        RelayCalendarGateway(),
        GoogleCalendarGateway(),
    ):
        assert isinstance(gateway, CalendarGateway)


# ---------------------------------------------------------------------------
# Relay: signing
# ---------------------------------------------------------------------------


def test_relay_signs_the_exact_transmitted_bytes_with_a_current_timestamp():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"busy": []})

    gateway, seen = _relay(handler)
    gateway.freebusy(utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 2, 0))

    request = seen[0]
    transmitted = request.read()

    # 1. The bytes on the wire are our own compact serialisation, not something
    #    httpx produced afterwards.  If httpx re-encoded, this would differ.
    expected_body = json.dumps(
        {
            "time_min": "2026-09-21T01:00:00+00:00",
            "time_max": "2026-09-21T02:00:00+00:00",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert transmitted == expected_body

    # 2. The signature is exactly what the server would compute from those bytes.
    timestamp = int(request.headers["X-Relay-Timestamp"])
    expected_signature = hmac.new(
        SECRET.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + transmitted,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-Relay-Signature"] == expected_signature

    # 3. The timestamp is current (within the relay's 300s freshness window).
    assert abs(timestamp - int(time.time())) < 5
    assert request.method == "POST"
    assert request.url.path == "/freebusy"


def test_relay_health_is_not_signed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    gateway, seen = _relay(handler)
    assert gateway.healthz() is True
    assert "X-Relay-Signature" not in seen[0].headers
    assert "X-Relay-Timestamp" not in seen[0].headers


# ---------------------------------------------------------------------------
# Relay: freebusy parsing
# ---------------------------------------------------------------------------


def test_relay_parses_busy_intervals_into_aware_utc():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "busy": [
                    {
                        "start": "2026-09-21T09:00:00+08:00",  # -> 01:00Z
                        "end": "2026-09-21T09:30:00+08:00",  # -> 01:30Z
                    }
                ]
            },
        )

    gateway, _ = _relay(handler)
    intervals = gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))

    assert intervals == [BusyInterval(start=utc(2026, 9, 21, 1, 0), end=utc(2026, 9, 21, 1, 30))]
    for interval in intervals:
        assert interval.start.tzinfo is not None
        assert interval.start.utcoffset() == timedelta(0)
        assert interval.end.utcoffset() == timedelta(0)


def test_relay_rejects_an_offset_less_busy_datetime():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"busy": [{"start": "2026-09-21T01:00:00", "end": "2026-09-21T01:30:00"}]},
        )

    gateway, _ = _relay(handler)
    with pytest.raises(CalendarGatewayError):
        gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))


def test_relay_rejects_naive_input_before_sending():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not send a naive datetime")

    gateway, seen = _relay(handler)
    with pytest.raises(CalendarGatewayError):
        gateway.freebusy(datetime(2026, 9, 21, 1, 0), utc(2026, 9, 21, 2, 0))
    assert seen == []


# ---------------------------------------------------------------------------
# Relay: errors
# ---------------------------------------------------------------------------


def test_relay_raises_on_500_carrying_detail_and_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "relay exploded"})

    gateway, _ = _relay(handler)
    with pytest.raises(CalendarGatewayError) as excinfo:
        gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))

    assert excinfo.value.detail == "relay exploded"
    assert excinfo.value.status_code == 500


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="this is not json"),
        httpx.Response(200, json={"nope": []}),
    ],
    ids=["non-json", "missing-busy"],
)
def test_relay_raises_on_a_malformed_body(response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    gateway, _ = _relay(handler)
    with pytest.raises(CalendarGatewayError):
        gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))


# ---------------------------------------------------------------------------
# Relay: events
# ---------------------------------------------------------------------------


def test_relay_create_hold_sends_transparent_false_and_returns_event():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["transparent"] is False
        assert payload["reference"] == "BK123"
        assert payload["start"] == "2026-09-21T01:00:00+00:00"
        return httpx.Response(
            200, json={"event_id": "evt-1", "html_link": "https://cal.example/evt-1"}
        )

    gateway, seen = _relay(handler)
    event = gateway.create_hold(
        summary="HOLD",
        description="desc",
        start=utc(2026, 9, 21, 1, 0),
        end=utc(2026, 9, 21, 1, 30),
        reference="BK123",
    )

    assert event == CalendarEvent(
        event_id="evt-1", html_link="https://cal.example/evt-1"
    )
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/events"


def test_relay_confirm_patches_the_event():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.read()) == {"summary": "s", "description": "d"}
        return httpx.Response(200, json={"event_id": "evt-1"})

    gateway, seen = _relay(handler)
    gateway.confirm("evt-1", summary="s", description="d")

    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/events/evt-1"


# ---------------------------------------------------------------------------
# release idempotency — one explicit test per adapter
# ---------------------------------------------------------------------------


def test_fake_release_unknown_id_is_idempotent():
    gateway = FakeCalendarGateway()
    gateway.release("never-existed")  # must not raise
    gateway.release("never-existed")  # and a retry must not either


def test_relay_release_404_is_idempotent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown event"})

    gateway, seen = _relay(handler)
    gateway.release("gone")  # must not raise
    gateway.release("gone")  # retry

    assert [request.method for request in seen] == ["DELETE", "DELETE"]
    assert seen[0].url.path == "/events/gone"


def test_relay_release_other_errors_still_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "relay exploded"})

    gateway, _ = _relay(handler)
    with pytest.raises(CalendarGatewayError):
        gateway.release("gone")


@pytest.mark.parametrize("status", [404, 410], ids=["404", "410"])
def test_google_release_missing_event_is_idempotent(status: int):
    service = _FakeGoogleService(delete_error=_http_error(status))
    gateway = GoogleCalendarGateway(service=service)
    gateway.release("gone")  # must not raise

    kind, kwargs = service.calls[0]
    assert kind == "events.delete"
    assert kwargs["eventId"] == "gone"


def test_google_release_other_errors_still_raise():
    service = _FakeGoogleService(delete_error=_http_error(500))
    gateway = GoogleCalendarGateway(service=service)
    with pytest.raises(CalendarGatewayError):
        gateway.release("gone")


# ---------------------------------------------------------------------------
# Fake semantics
# ---------------------------------------------------------------------------


def test_fake_hold_blocks_the_interval_in_a_subsequent_freebusy():
    gateway = FakeCalendarGateway()
    assert gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0)) == []

    hold = gateway.create_hold(
        summary="HOLD",
        description="d",
        start=utc(2026, 9, 21, 1, 0),
        end=utc(2026, 9, 21, 1, 30),
        reference="BK1",
    )

    busy = gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))
    assert busy == [BusyInterval(start=utc(2026, 9, 21, 1, 0), end=utc(2026, 9, 21, 1, 30))]

    # Releasing the hold frees the slot again; a second release is a no-op.
    gateway.release(hold.event_id)
    gateway.release(hold.event_id)
    assert gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0)) == []


def test_fake_add_busy_is_reflected_and_only_overlaps_are_returned():
    gateway = FakeCalendarGateway()
    gateway.add_busy(utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 2, 0))

    overlapping = gateway.freebusy(utc(2026, 9, 21, 1, 30), utc(2026, 9, 21, 3, 0))
    assert overlapping == [BusyInterval(utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 2, 0))]

    disjoint = gateway.freebusy(utc(2026, 9, 21, 3, 0), utc(2026, 9, 21, 4, 0))
    assert disjoint == []


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def test_google_freebusy_parses_aware_utc():
    service = _FakeGoogleService(
        freebusy_result={
            "calendars": {
                "primary": {
                    "busy": [
                        {
                            "start": "2026-09-21T09:00:00+08:00",
                            "end": "2026-09-21T09:30:00+08:00",
                        }
                    ]
                }
            }
        }
    )
    gateway = GoogleCalendarGateway(service=service)
    intervals = gateway.freebusy(utc(2026, 9, 21, 0, 0), utc(2026, 9, 21, 3, 0))

    assert intervals == [BusyInterval(utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 30))]
    assert intervals[0].start.utcoffset() == timedelta(0)


def test_google_create_hold_sets_opaque_transparency():
    service = _FakeGoogleService(
        insert_result={"id": "g-1", "htmlLink": "https://cal.google/g-1"}
    )
    gateway = GoogleCalendarGateway(service=service)
    event = gateway.create_hold(
        summary="HOLD",
        description="d",
        start=utc(2026, 9, 21, 1, 0),
        end=utc(2026, 9, 21, 1, 30),
        reference="BK1",
    )

    assert event == CalendarEvent(event_id="g-1", html_link="https://cal.google/g-1")
    kind, kwargs = service.calls[0]
    assert kind == "events.insert"
    assert kwargs["body"]["transparency"] == "opaque"


def test_google_confirm_patches_to_confirmed():
    service = _FakeGoogleService(patch_result={"id": "g-1"})
    gateway = GoogleCalendarGateway(service=service)
    gateway.confirm("g-1", summary="s", description="d")

    kind, kwargs = service.calls[0]
    assert kind == "events.patch"
    assert kwargs["eventId"] == "g-1"
    assert kwargs["body"]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# A fake Google service object, so no socket is ever opened
# ---------------------------------------------------------------------------


class _FakeGoogleRequest:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeGoogleService:
    def __init__(
        self,
        *,
        freebusy_result=None,
        freebusy_error: Exception | None = None,
        insert_result=None,
        patch_result=None,
        delete_error: Exception | None = None,
    ) -> None:
        self._freebusy_result = freebusy_result
        self._freebusy_error = freebusy_error
        self._insert_result = insert_result
        self._patch_result = patch_result
        self._delete_error = delete_error
        self.calls: list[tuple[str, dict]] = []

    def freebusy(self):
        return self

    def events(self):
        return self

    def query(self, body):
        self.calls.append(("freebusy.query", body))
        return _FakeGoogleRequest(self._freebusy_result, self._freebusy_error)

    def insert(self, **kwargs):
        self.calls.append(("events.insert", kwargs))
        return _FakeGoogleRequest(self._insert_result)

    def patch(self, **kwargs):
        self.calls.append(("events.patch", kwargs))
        return _FakeGoogleRequest(self._patch_result)

    def delete(self, **kwargs):
        self.calls.append(("events.delete", kwargs))
        return _FakeGoogleRequest(None, self._delete_error)
