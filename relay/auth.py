"""HMAC request authentication for the relay HTTP contract (contract §9).

Wire format::

    X-Relay-Timestamp: <unix seconds>
    X-Relay-Signature: hex(hmac_sha256(secret, f"{timestamp}.{raw_body}"))

The signature is over the *exact bytes received*, so the dependency reads the
raw body once and hands those same bytes to the handler — the handler must never
re-serialise a parsed model and expect it to verify.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Request

from relay.config import Settings, get_settings

MAX_SKEW_SECONDS = 300
TIMESTAMP_HEADER = "X-Relay-Timestamp"
SIGNATURE_HEADER = "X-Relay-Signature"


def build_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    """``hex(hmac_sha256(secret, f"{timestamp}.{raw_body}"))`` — contract §9.

    This is also the client-side helper: the booking app's ``RelayCalendarGateway``
    signs with exactly this construction, and the tests sign with it too, so the
    wire format has a single definition.
    """
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail)


async def require_relay_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> bytes:
    """Verify the HMAC over the raw request body; return those raw bytes.

    Every endpoint except ``GET /healthz`` depends on this.  Failures are ``401``
    with ``{"detail": str}``.
    """
    body = await request.body()
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    signature = request.headers.get(SIGNATURE_HEADER)

    if not timestamp or not signature:
        raise _unauthorized(f"missing {TIMESTAMP_HEADER} or {SIGNATURE_HEADER}")

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise _unauthorized(f"invalid {TIMESTAMP_HEADER}") from None

    if abs(int(time.time()) - ts) > MAX_SKEW_SECONDS:
        raise _unauthorized(f"stale timestamp: skew exceeds {MAX_SKEW_SECONDS}s")

    expected = build_signature(settings.relay_secret, ts, body)
    if not hmac.compare_digest(expected, signature):
        raise _unauthorized("bad signature")

    return body
