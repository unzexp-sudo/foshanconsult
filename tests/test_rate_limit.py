"""Rate limiting — BUILD_PLAN §10.

The booking endpoint is the one that matters: every accepted call creates a
calendar hold **and** a WeChat Pay order, so an unauthenticated loop can burn
Google's freebusy quota and fill WeChat with unpaid orders.  These tests pin four
properties that a naive implementation gets wrong:

* the counter counts *attempts*, not successes — otherwise a caller just spams
  requests that were going to fail anyway and pays no budget for them;
* a throttled caller is refused even for a slot that is genuinely free, so a 429 is
  never a business rejection wearing a costume;
* two endpoints have two budgets, so a burst on ``/api/slots`` cannot lock a real
  customer out of ``POST /api/bookings``;
* the notify endpoint is never throttled, because WeChat's retry schedule is part
  of the payment protocol and a 429 would strand a paid booking.

Written by the integrator, so it may touch anything.  It uses only the public HTTP
surface plus the limiter's own unit interface.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app as fastapi_app
from app.rate_limit import FixedWindowLimiter, client_ip
from app.routers.payments import NOTIFY_PATH

SHANGHAI = ZoneInfo("Asia/Shanghai")

BOOKING_LIMIT = settings.rate_limit_bookings_per_hour
SLOTS_LIMIT = settings.rate_limit_slots_per_hour


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def next_weekday_local(days_ahead: int) -> date:
    """The first Mon–Fri local date at least ``days_ahead`` days out."""
    day = (datetime.now(UTC).astimezone(SHANGHAI) + timedelta(days=days_ahead)).date()
    for _ in range(14):
        if day.weekday() <= 4:
            return day
        day += timedelta(days=1)
    raise AssertionError("no weekday found in the next two weeks")


def next_bookable_local(days_ahead: int = 2) -> datetime:
    """14:00 Shanghai on an upcoming weekday."""
    return datetime.combine(next_weekday_local(days_ahead), time(14, 0), tzinfo=SHANGHAI)


def bookable_slots(count: int) -> list[datetime]:
    """``count`` distinct slots that cannot collide with each other.

    Spaced 30 minutes apart from 09:00, so each booking leaves the next slot
    untouched.  That is the point: the only reason a request may fail in these tests
    is the throttle, never a slot clash or a duration overlap.
    """
    day = next_weekday_local(2)
    slots = [
        datetime.combine(day, time(9, 0), tzinfo=SHANGHAI) + timedelta(minutes=30 * i)
        for i in range(count)
    ]
    assert slots[-1].astimezone(UTC) > datetime.now(UTC) + timedelta(hours=4)
    return slots


def payload(slot_local: datetime, **overrides) -> dict:
    body = {
        "event_type_id": "consult-30",
        "slot_start": slot_local.isoformat(),
        "customer_name": "Alice",
        "customer_email": "alice@example.com",
    }
    body.update(overrides)
    return body


def post_booking(client: TestClient, slot_local: datetime):
    return client.post("/api/bookings", json=payload(slot_local))


def spend_booking_budget(client: TestClient) -> None:
    """Use up the booking budget on real, successful bookings."""
    for slot in bookable_slots(BOOKING_LIMIT):
        response = post_booking(client, slot)
        assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# The limiter itself
# ---------------------------------------------------------------------------


def test_allows_exactly_the_limit_then_refuses():
    limiter = FixedWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.hit("k", now=0.0)[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.hit("k", now=0.0)
    assert allowed is False
    assert retry_after > 0


def test_window_rolls_over_after_it_elapses():
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)

    assert limiter.hit("k", now=0.0)[0] is True
    assert limiter.hit("k", now=59.9)[0] is False
    # A fixed window restarts wholesale at the boundary; the next hit opens a new one.
    assert limiter.hit("k", now=60.0)[0] is True


def test_retry_after_never_exceeds_the_window():
    """Advertising a longer wait than the window is a lie the caller can catch."""
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)
    limiter.hit("k", now=100.0)

    _, retry_after = limiter.hit("k", now=110.0)
    assert 0 < retry_after <= 60


def test_keys_are_independent():
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)

    assert limiter.hit("a", now=0.0)[0] is True
    assert limiter.hit("b", now=0.0)[0] is True
    assert limiter.hit("a", now=0.0)[0] is False


def test_reset_forgets_every_counter():
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)
    limiter.hit("k", now=0.0)
    limiter.reset()
    assert limiter.hit("k", now=0.0)[0] is True


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (1, 0), (1, -5)])
def test_rejects_a_configuration_that_could_never_limit(limit: int, window: int):
    """A zero limit would 429 every request; a zero window would never reset."""
    with pytest.raises(ValueError):
        FixedWindowLimiter(limit=limit, window_seconds=window)


def test_prunes_dead_windows_once_the_table_grows(monkeypatch):
    monkeypatch.setattr(FixedWindowLimiter, "_PRUNE_AT", 4)
    limiter = FixedWindowLimiter(limit=1, window_seconds=10)

    for index in range(4):
        limiter.hit(f"k{index}", now=0.0)
    # Every earlier window is long dead by now; the sweep should reclaim them.
    limiter.hit("fresh", now=1000.0)

    assert len(limiter._hits) == 1


# ---------------------------------------------------------------------------
# Client identity — the part that decides whether the limit means anything
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Just enough of ``Request`` for ``client_ip``."""

    def __init__(self, peer: str, headers: dict[str, str] | None = None) -> None:
        self.client = type("Peer", (), {"host": peer})()
        self.headers = headers or {}


def test_uses_the_peer_address_when_no_proxy_is_trusted():
    request = _FakeRequest("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert client_ip(request, trusted_proxy_depth=0) == "203.0.113.9"


def test_rotating_a_forged_header_cannot_mint_identities():
    """The whole limit collapses if the caller can name itself."""
    seen = {
        client_ip(
            _FakeRequest("203.0.113.9", {"x-forwarded-for": f"10.0.0.{i}"}),
            trusted_proxy_depth=0,
        )
        for i in range(50)
    }
    assert seen == {"203.0.113.9"}


def test_reads_the_client_from_the_right_of_the_chain():
    """One trusted proxy: it appends the real address last, so take the last hop."""
    request = _FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 198.51.100.7"})
    assert client_ip(request, trusted_proxy_depth=1) == "198.51.100.7"


def test_forged_leftmost_hop_does_not_change_identity_behind_a_proxy():
    real = "198.51.100.7"
    identities = {
        client_ip(
            _FakeRequest("10.0.0.1", {"x-forwarded-for": f"10.9.9.{i}, {real}"}),
            trusted_proxy_depth=1,
        )
        for i in range(50)
    }
    assert identities == {real}


def test_falls_back_to_the_peer_when_the_chain_is_shorter_than_claimed():
    """A short header means the request did not come through our proxy."""
    request = _FakeRequest("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert client_ip(request, trusted_proxy_depth=2) == "203.0.113.9"


# ---------------------------------------------------------------------------
# POST /api/bookings
# ---------------------------------------------------------------------------


def test_booking_is_refused_once_the_hourly_budget_is_spent(client, event_type):
    assert BOOKING_LIMIT >= 2, "the test env limit is too low to be meaningful"
    slots = bookable_slots(BOOKING_LIMIT + 1)

    for attempt, slot in enumerate(slots[:BOOKING_LIMIT]):
        response = post_booking(client, slot)
        assert response.status_code == 201, f"attempt {attempt}: {response.text}"

    refused = post_booking(client, slots[-1])
    assert refused.status_code == 429, refused.text
    assert refused.headers.get("retry-after")
    assert 0 < int(refused.headers["retry-after"]) <= 3600


def test_the_budget_is_spent_by_failed_attempts_too(client, event_type):
    """Counting only successes would let a caller spam losing requests for free."""
    slot = next_bookable_local()
    assert post_booking(client, slot).status_code == 201

    for attempt in range(BOOKING_LIMIT - 1):
        response = post_booking(client, slot)
        assert response.status_code == 409, f"attempt {attempt}: {response.text}"

    # The slot was never bookable again, yet the budget is gone.
    assert post_booking(client, slot).status_code == 429


def test_a_throttled_caller_cannot_book_a_free_slot(client, event_type):
    """429 must be a throttle, not a disguised business rejection."""
    spend_booking_budget(client)

    elsewhere = bookable_slots(BOOKING_LIMIT + 1)[-1]
    assert post_booking(client, elsewhere).status_code == 429


def test_a_second_caller_keeps_its_own_budget(client, event_type):
    """One noisy IP must not lock out everybody else."""
    slots = bookable_slots(BOOKING_LIMIT)

    with TestClient(fastapi_app, client=("203.0.113.9", 40001)) as noisy:
        for slot in slots:
            assert noisy.post("/api/bookings", json=payload(slot)).status_code == 201
        assert noisy.post("/api/bookings", json=payload(slots[0])).status_code == 429

    # 409 means "that slot is gone" — i.e. the request was actually processed, so
    # this caller was never throttled.
    with TestClient(fastapi_app, client=("198.51.100.7", 40002)) as quiet:
        assert quiet.post("/api/bookings", json=payload(slots[0])).status_code == 409


# ---------------------------------------------------------------------------
# GET /api/slots
# ---------------------------------------------------------------------------


def _slot_params() -> dict[str, str]:
    return {
        "event_type_id": "consult-30",
        "date": next_bookable_local().date().isoformat(),
    }


def test_slots_are_throttled_at_their_own_limit(client, event_type):
    for _ in range(SLOTS_LIMIT):
        assert client.get("/api/slots", params=_slot_params()).status_code == 200

    assert client.get("/api/slots", params=_slot_params()).status_code == 429


def test_the_slots_budget_is_separate_from_the_bookings_budget(client, event_type):
    """Browsing dates must never cost a customer their ability to book."""
    for _ in range(SLOTS_LIMIT + 1):
        client.get("/api/slots", params=_slot_params())
    assert client.get("/api/slots", params=_slot_params()).status_code == 429

    assert post_booking(client, bookable_slots(1)[0]).status_code == 201


# ---------------------------------------------------------------------------
# The notify endpoint must stay unthrottled
# ---------------------------------------------------------------------------


def test_wechat_notify_is_never_throttled(client, event_type):
    """WeChat retries a callback up to 15 times; refusing one loses the payment.

    These bodies are deliberately unsigned and malformed — the point is only that a
    flood of them never starts returning 429, which is what would happen if the
    generic throttle were applied here too.
    """
    for attempt in range(60):
        response = client.post(NOTIFY_PATH, content=b"{}")
        assert response.status_code != 429, f"attempt {attempt} was throttled"
