"""Request throttling for the endpoints that cost money or third-party quota.

BUILD_PLAN §10: *"Rate-limit ``POST /api/bookings`` — it creates a calendar event
and a payment order per call."*  One unauthenticated loop is otherwise enough to
burn the Google Calendar freebusy quota and fill WeChat Pay with unpaid orders.

What is throttled, and why:

* ``POST /api/bookings`` — the expensive one.  A calendar hold **and** a WeChat
  order per accepted call.
* ``GET /api/slots`` — one calendar fan-out per call.  This is *beyond* what §10
  asks for; it is here because exhausting the freebusy quota breaks booking for
  everybody, and the limit is deliberately loose enough that a human browsing a
  month of dates never notices it.

What is deliberately **not** throttled: ``POST /api/payments/wechat/notify``.
WeChat's retry schedule is part of the payment protocol — answering a retry with
429 would strand a paid booking that WeChat can no longer tell us about.  That
endpoint is protected by signature verification and by
``PaymentEvent.transaction_id`` uniqueness instead.

Two honest limitations of this implementation:

1. **The counters live in this process.**  With ``--workers 2``, or two replicas,
   the real budget is the configured limit times the number of processes.
   ``Dockerfile`` starts a single uvicorn worker, so a one-replica deploy
   is exact; anything else is approximate.  A shared store (Redis, or a table) is
   the fix when that stops being true.
2. **It is a fixed window, not a sliding one,** so a caller can burst up to
   roughly twice the limit across a window boundary.  That is fine for abuse
   control and it is *not* a billing primitive.

Both are the right trade for v1: no new dependency, no shared store, no migration.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.config import settings

__all__ = [
    "LIMITERS",
    "FixedWindowLimiter",
    "client_ip",
    "limit_dependency",
    "rate_limit_bookings",
    "rate_limit_slots",
]

_WINDOW_SECONDS = 3600


class FixedWindowLimiter:
    """Allow ``limit`` hits per ``window_seconds`` for each distinct key.

    Thread-safe: sync FastAPI endpoints run in a worker threadpool, so two
    requests genuinely do arrive at once.
    """

    #: Above this many tracked keys, drop the dead windows.  Opportunistic — a
    #: sweep on every hit would be O(n) per request.
    _PRUNE_AT = 10_000

    def __init__(self, limit: int, window_seconds: int = _WINDOW_SECONDS) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        # key -> (window_start, hits_so_far)
        self._hits: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        """Count one hit against ``key``.

        Returns ``(allowed, retry_after_seconds)``.  ``retry_after`` is 0 when the
        hit is allowed.  ``now`` is a ``time.monotonic()`` reading, injectable so
        tests can roll the window without sleeping.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(moment)
            window_start, count = self._hits.get(key, (moment, 0))
            if moment - window_start >= self.window_seconds:
                window_start, count = moment, 0
            count += 1
            self._hits[key] = (window_start, count)
            if count <= self.limit:
                return True, 0
            remaining = self.window_seconds - (moment - window_start)
            # Ceil, and never past the window: a Retry-After longer than the window
            # is a lie the caller can catch us in.
            return False, max(1, math.ceil(remaining))

    def reset(self) -> None:
        """Forget every counter.  Used by the test suite between tests."""
        with self._lock:
            self._hits.clear()

    def _prune(self, now: float) -> None:
        if len(self._hits) < self._PRUNE_AT:
            return
        self._hits = {
            key: value
            for key, value in self._hits.items()
            if now - value[0] < self.window_seconds
        }


def client_ip(request: Request, *, trusted_proxy_depth: int = 0) -> str:
    """Best-effort caller identity, safe against header spoofing.

    With ``trusted_proxy_depth == 0`` we use the peer address, which a caller
    cannot forge.  Behind N trusted proxies the caller is the **Nth entry from the
    right** of ``X-Forwarded-For``.

    Counting from the right matters: the leftmost entry is whatever the caller
    sent, so a naive "take the first IP" implementation lets an attacker mint a
    fresh identity per request by rotating the header, and the limiter stops
    limiting anything at all.  When the chain is shorter than the configured depth
    the header is not trustworthy and we fall back to the peer address.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_depth <= 0:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    index = len(hops) - trusted_proxy_depth
    if 0 <= index < len(hops):
        return hops[index]
    return peer


def limit_dependency(
    scope: str,
    limiter: FixedWindowLimiter,
    *,
    trusted_proxy_depth: int = 0,
) -> Callable[[Request], None]:
    """Build a FastAPI dependency that enforces ``limiter`` under ``scope``."""

    def dependency(request: Request) -> None:
        key = f"{scope}:{client_ip(request, trusted_proxy_depth=trusted_proxy_depth)}"
        allowed, retry_after = limiter.hit(key)
        if allowed:
            return
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many requests; please wait a moment and try again",
            headers={"Retry-After": str(retry_after)},
        )

    return dependency


# ---------------------------------------------------------------------------
# The application's limiters, built from settings at import time
# ---------------------------------------------------------------------------

bookings_limiter = FixedWindowLimiter(settings.rate_limit_bookings_per_hour)
slots_limiter = FixedWindowLimiter(settings.rate_limit_slots_per_hour)

#: Every limiter, so the test suite can reset all of them in one call.
LIMITERS: tuple[FixedWindowLimiter, ...] = (bookings_limiter, slots_limiter)

rate_limit_bookings = limit_dependency(
    "bookings",
    bookings_limiter,
    trusted_proxy_depth=settings.trusted_proxy_depth,
)
rate_limit_slots = limit_dependency(
    "slots",
    slots_limiter,
    trusted_proxy_depth=settings.trusted_proxy_depth,
)
