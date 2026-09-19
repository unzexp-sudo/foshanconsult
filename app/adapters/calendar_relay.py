"""HTTP client for the standalone calendar relay (contract §9).

The booking app never holds a Google credential in production; this adapter is
the only path from the app to a calendar.  Two things are load-bearing:

* **The signature covers the exact bytes on the wire.**  The body is serialised
  here, signed, and handed to httpx as ``content=`` bytes so httpx can never
  re-encode it after signing.
* **``release`` is idempotent.**  A ``404`` means "already gone", not "error" —
  release is retried, so turning it into an error would make retries fail.

Construction with no arguments reads the settings singleton (that is how
``app.deps`` builds it); tests inject ``base_url``/``secret``/``client``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.config import settings
from app.ports.calendar import BusyInterval, CalendarEvent, CalendarGatewayError

DEFAULT_TIMEOUT = 10.0


def sign(secret: str, timestamp: int, raw_body: bytes) -> str:
    """``hex(hmac_sha256(secret, f"{timestamp}.{raw_body}"))`` over exact bytes."""
    message = str(timestamp).encode("ascii") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _serialise(payload: dict) -> bytes:
    """Deterministic JSON bytes — must be the bytes that get signed and sent."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _iso(value: datetime) -> str:
    """ISO-8601 with an explicit offset.  Naive input is a bug, not a default."""
    if value.tzinfo is None:
        raise CalendarGatewayError("naive datetime passed to relay calendar gateway")
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: object, field: str) -> datetime:
    """Parse a relay datetime; an offset-less one is a bug, not a default."""
    if not isinstance(value, str):
        raise CalendarGatewayError(f"relay busy interval has non-string {field!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CalendarGatewayError(f"relay sent unparseable {field!r}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CalendarGatewayError(f"relay sent offset-less {field!r}: {value!r}")
    return parsed.astimezone(UTC)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return str(body)


class RelayCalendarGateway:
    """Signed ``httpx`` client for the relay's frozen HTTP contract."""

    def __init__(
        self,
        base_url: str | None = None,
        secret: str | None = None,
        client: httpx.Client | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (base_url if base_url is not None else settings.calendar_relay_url) or ""
        self._secret = secret if secret is not None else settings.calendar_relay_secret
        self._client = client
        self._timeout = timeout

    # -- transport ----------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _request(
        self, method: str, path: str, payload: dict | None = None, *, signed: bool = True
    ) -> httpx.Response:
        raw = _serialise(payload) if payload is not None else b""
        headers: dict[str, str] = {}
        if signed:
            timestamp = int(time.time())
            headers["X-Relay-Timestamp"] = str(timestamp)
            headers["X-Relay-Signature"] = sign(self._secret, timestamp, raw)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self._base_url.rstrip('/')}{path}"
        try:
            return self._http().request(method, url, content=raw, headers=headers)
        except httpx.HTTPError as exc:
            raise CalendarGatewayError(f"relay request failed: {exc}") from exc

    @staticmethod
    def _check(response: httpx.Response, *, allow_404: bool = False) -> None:
        if allow_404 and response.status_code == 404:
            return
        if 200 <= response.status_code < 300:
            return
        detail = _detail(response)
        error = CalendarGatewayError(f"relay {response.status_code}: {detail}")
        error.detail = detail  # type: ignore[attr-defined]
        error.status_code = response.status_code  # type: ignore[attr-defined]
        raise error

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise CalendarGatewayError("relay returned malformed JSON") from exc

    # -- CalendarGateway ----------------------------------------------------

    def healthz(self) -> bool:
        """``GET /healthz`` — the one request that is not signed."""
        response = self._request("GET", "/healthz", signed=False)
        self._check(response)
        data = self._json(response)
        return bool(isinstance(data, dict) and data.get("ok"))

    def freebusy(self, time_min: datetime, time_max: datetime) -> list[BusyInterval]:
        response = self._request(
            "POST",
            "/freebusy",
            {"time_min": _iso(time_min), "time_max": _iso(time_max)},
        )
        self._check(response)
        data = self._json(response)
        busy = data.get("busy") if isinstance(data, dict) else None
        if not isinstance(busy, list):
            raise CalendarGatewayError("relay /freebusy response missing 'busy' list")
        intervals: list[BusyInterval] = []
        for item in busy:
            if not isinstance(item, dict) or "start" not in item or "end" not in item:
                raise CalendarGatewayError("relay /freebusy sent a malformed busy interval")
            intervals.append(
                BusyInterval(
                    start=_parse_utc(item["start"], "start"),
                    end=_parse_utc(item["end"], "end"),
                )
            )
        return intervals

    def create_hold(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        reference: str,
    ) -> CalendarEvent:
        response = self._request(
            "POST",
            "/events",
            {
                "summary": summary,
                "description": description,
                "start": _iso(start),
                "end": _iso(end),
                "reference": reference,
                "transparent": False,  # a hold must block the slot
            },
        )
        self._check(response)
        data = self._json(response)
        event_id = data.get("event_id") if isinstance(data, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise CalendarGatewayError("relay /events response missing 'event_id'")
        html_link = data.get("html_link")
        return CalendarEvent(
            event_id=event_id,
            html_link=html_link if isinstance(html_link, str) else None,
        )

    def confirm(self, event_id: str, *, summary: str, description: str) -> None:
        response = self._request(
            "PATCH",
            f"/events/{quote(event_id, safe='')}",
            {"summary": summary, "description": description},
        )
        self._check(response)

    def release(self, event_id: str) -> None:
        response = self._request("DELETE", f"/events/{quote(event_id, safe='')}")
        # 404 = already released.  Swallowed so retried releases stay safe.
        self._check(response, allow_404=True)
