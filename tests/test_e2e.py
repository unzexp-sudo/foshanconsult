"""End-to-end suite across module seams (M7) — contract §11, BUILD_PLAN §8/§9 P7.

Everything here drives the real HTTP surface with ``TestClient``.  Where a seam
matters the **real** adapter is used rather than the fake:

* the WeChat Pay callback is a genuinely RSA-signed, AES-256-GCM-encrypted
  notification built by ``tests.fakes.make_wechat_notify`` and verified by the
  real ``app.adapters.payments_wechat.WechatPayGateway``;
* the outbound Native order goes through that same real adapter, with only its
  ``httpx`` transport mocked, so the request shape (amount, notify_url) is the
  production one and no socket is ever opened.

No network.  ``CALENDAR_GATEWAY`` and ``EMAIL_BACKEND`` stay on the shared fakes.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import deps as app_deps
from app.adapters.calendar_relay import RelayCalendarGateway
from app.adapters.payments_wechat import WechatPayGateway
from app.deps import get_calendar_gateway, get_payment_gateway
from app.main import app
from app.models import Booking, BookingStatus, PaymentEvent
from relay import config as relay_config
from relay.google import GoogleEventNotFound, InsertedEvent
from relay.main import app as relay_app
from relay.main import create_app as create_relay_app
from tests.fakes import make_wechat_notify, wechat_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")

# Monday 2026-09-21.  NOW is Mon 09:00 Shanghai; consult-30 needs 240 min notice,
# so 14:00 Shanghai (06:00 UTC) is the first comfortable bookable slot.
NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
LOCAL_DATE = "2026-09-21"
SLOT_LOCAL = "2026-09-21T14:00:00+08:00"
SLOT_UTC = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)

NOTIFY_URL = "/api/payments/wechat/notify"
CODE_URL = "weixin://wxpay/bizpayurl?pr=E2ETESTCODEURL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def payload(slot, **overrides) -> dict:
    body = {
        "event_type_id": "consult-30",
        "slot_start": slot.isoformat() if isinstance(slot, datetime) else slot,
        "customer_name": "Alice",
        "customer_email": "alice@example.com",
    }
    body.update(overrides)
    return body


def offered_slot_starts(response) -> list[datetime]:
    """Parse the API's slot starts.

    FastAPI serialises an aware-UTC datetime with a trailing ``Z``, so comparing
    raw strings against ``datetime.isoformat()`` (which yields ``+00:00``) is a
    silent mismatch.  Compare datetimes.
    """
    return [
        datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        for item in response.json()["slots"]
    ]


def reload_booking(db, reference: str) -> Booking:
    db.expire_all()
    row = db.scalar(select(Booking).where(Booking.reference == reference))
    assert row is not None
    return row


def post_signed_notify(client, keys, *, out_trade_no: str, amount_fen: int, **kwargs):
    headers, body = make_wechat_notify(
        keys=keys, out_trade_no=out_trade_no, amount_fen=amount_fen, **kwargs
    )
    return client.post(NOTIFY_URL, content=body, headers=headers)


def next_bookable_local() -> datetime:
    """14:00 Shanghai on the next weekday that clears the 4h notice window.

    The race test cannot freeze the clock (freezegun and threads do not mix), so
    it derives a slot from the real clock instead.
    """
    now_utc = datetime.now(UTC)
    day = (now_utc.astimezone(SHANGHAI) + timedelta(days=2)).date()
    for _ in range(14):
        if day.weekday() <= 4:
            candidate = datetime.combine(day, time(14, 0), tzinfo=SHANGHAI)
            if candidate.astimezone(UTC) > now_utc + timedelta(hours=4):
                return candidate
        day += timedelta(days=1)
    raise AssertionError("no bookable weekday found in the next two weeks")


def new_transaction_id() -> str:
    return "4200001" + uuid.uuid4().hex[:14]


class FakeGoogleCalendar:
    """Minimal in-process Google Calendar: a non-transparent event blocks time."""

    def __init__(self) -> None:
        self.busy: list[tuple[str, str]] = []
        self.events: dict[str, dict[str, str]] = {}
        self._counter = 0

    def freebusy(self, time_min: str, time_max: str) -> list[dict[str, str]]:
        low = datetime.fromisoformat(time_min)
        high = datetime.fromisoformat(time_max)
        return [
            {"start": start, "end": end}
            for start, end in self.busy
            if datetime.fromisoformat(start) < high and datetime.fromisoformat(end) > low
        ]

    def insert_event(self, *, summary, description, start, end, reference, transparent):
        self._counter += 1
        event_id = f"google-{self._counter}"
        self.events[event_id] = {
            "summary": summary,
            "description": description,
            "start": start,
            "end": end,
        }
        if not transparent:
            self.busy.append((start, end))
        return InsertedEvent(event_id=event_id, html_link=f"https://cal.example/{event_id}")

    def patch_event(self, event_id: str, summary: str, description: str) -> None:
        if event_id not in self.events:
            raise GoogleEventNotFound(event_id)
        self.events[event_id].update(summary=summary, description=description)

    def delete_event(self, event_id: str) -> None:
        if event_id not in self.events:
            raise GoogleEventNotFound(event_id)
        event = self.events.pop(event_id)
        self.busy = [item for item in self.busy if item != (event["start"], event["end"])]


# ---------------------------------------------------------------------------
# Wiring: the real WeChat adapter + fakes the background tasks can see
# ---------------------------------------------------------------------------


@pytest.fixture
def wechat_sent() -> list[dict]:
    """Every Native order body the real adapter actually sent."""
    return []


@pytest.fixture
def wechat_gateway(wechat_keys, wechat_sent) -> WechatPayGateway:
    """The REAL adapter with only its transport mocked — no socket, no network."""

    def handler(request: httpx.Request) -> httpx.Response:
        wechat_sent.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code_url": CODE_URL, "prepay_id": "wx-prepay-0001"})

    return WechatPayGateway(
        config=wechat_settings(wechat_keys),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def wire(monkeypatch):
    """Point both dependency paths at the given gateways.

    ``app.tasks`` resolves its gateways by calling ``app.deps.get_*`` *directly* —
    not through FastAPI's dependency injection — so ``dependency_overrides`` alone
    is invisible to the background finalize.  Patching the module attribute as well
    is what lets this suite observe the calendar confirm and the confirmation email.
    """

    def _wire(*, calendar, payments, email) -> None:
        app.dependency_overrides[get_calendar_gateway] = lambda: calendar
        app.dependency_overrides[get_payment_gateway] = lambda: payments
        monkeypatch.setattr(app_deps, "get_calendar_gateway", lambda: calendar)
        monkeypatch.setattr(app_deps, "get_payment_gateway", lambda: payments)
        monkeypatch.setattr(app_deps, "get_email_sender", lambda: email)

    return _wire


@pytest.fixture
def e2e(client, fake_calendar, recording_email, wechat_gateway, wire):
    """The shared client, the real WeChat adapter, and tasks wired to the same fakes."""
    wire(calendar=fake_calendar, payments=wechat_gateway, email=recording_email)
    yield client


RELAY_SECRET = "e2e-relay-secret"


@pytest.fixture
def relay_seam(monkeypatch):
    """The booking app's REAL relay client, in-process against the REAL relay app.

    The adapter's signed bytes are forwarded verbatim, so the HMAC construction of
    contract §9 is exercised over the real wire format with no socket.  Google
    itself is a local fake behind the relay's own ``create_app`` seam.
    """
    monkeypatch.setattr(relay_config.settings, "relay_secret", RELAY_SECRET)

    google = FakeGoogleCalendar()
    relay_client = TestClient(create_relay_app(google_client=google))

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            name: request.headers[name]
            for name in ("Content-Type", "X-Relay-Timestamp", "X-Relay-Signature")
            if name in request.headers
        }
        forwarded = relay_client.request(
            request.method, request.url.path, content=request.content, headers=headers
        )
        return httpx.Response(
            forwarded.status_code,
            content=forwarded.content,
            headers={"Content-Type": "application/json"},
        )

    gateway = RelayCalendarGateway(
        base_url="http://relay.test",
        secret=RELAY_SECRET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return gateway, google, relay_client


# ---------------------------------------------------------------------------
# P7 acceptance: seed → slots → book → REAL signed callback → paid
# ---------------------------------------------------------------------------


def test_full_happy_path_from_event_types_to_paid(
    e2e,
    event_type,
    db_session,
    fake_calendar,
    recording_email,
    wechat_keys,
    wechat_sent,
    freeze_now,
):
    with freeze_now(NOW):
        listed_types = e2e.get("/api/event-types")
        assert listed_types.status_code == 200
        assert [item["id"] for item in listed_types.json()] == ["consult-30"]
        assert listed_types.json()[0]["price_fen"] == 50000

        listed = e2e.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": LOCAL_DATE}
        )
        assert listed.status_code == 200
        assert SLOT_UTC in offered_slot_starts(listed)

        created = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        assert created.status_code == 201, created.text
        body = created.json()
        reference = body["reference"]
        assert body["status"] == "pending_payment"
        assert body["amount_fen"] == 50000
        assert body["currency"] == "CNY"
        assert body["code_url"] == CODE_URL

        # The order WeChat was actually asked to create, through the real adapter.
        assert len(wechat_sent) == 1
        order = wechat_sent[-1]
        assert order["amount"] == {"total": 50000, "currency": "CNY"}
        assert order["out_trade_no"] == reference
        assert order["notify_url"] == "http://testserver/api/payments/wechat/notify"

        hold_event_id = next(iter(fake_calendar.events))
        assert fake_calendar.events[hold_event_id]["status"] == "tentative"

        # The QR page renders markup, never the payment token.
        pending_page = e2e.get(f"/book/{reference}")
        assert pending_page.status_code == 200
        assert "<svg" in pending_page.text
        assert CODE_URL not in pending_page.text
        assert "weixin://" not in pending_page.text

        transaction_id = new_transaction_id()
        notify = post_signed_notify(
            e2e,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=transaction_id,
        )
        assert notify.status_code == 200
        assert notify.json() == {"code": "SUCCESS", "message": "成功"}

        status = e2e.get(f"/api/bookings/{reference}")
        assert status.status_code == 200
        assert status.json()["status"] == "paid"
        assert "code_url" not in status.json()  # the polling endpoint must not leak it

        row = reload_booking(db_session, reference)
        assert row.status is BookingStatus.PAID
        assert row.provider_transaction_id == transaction_id
        assert row.paid_at is not None

        # finalize_paid_booking ran as a background task: calendar confirmed + email.
        assert fake_calendar.confirmed == [hold_event_id]
        assert fake_calendar.events[hold_event_id]["status"] == "confirmed"
        assert len(recording_email.sent) == 1
        message = recording_email.sent[0]
        assert message["to"] == "alice@example.com"
        assert reference in message["subject"]
        assert reference in message["body"]
        assert "¥500.00" in message["body"]

        paid_page = e2e.get(f"/book/{reference}")
        assert "预约已确认" in paid_page.text
        assert CODE_URL not in paid_page.text
        assert "weixin://" not in paid_page.text


def test_status_page_never_contains_the_raw_code_url(e2e, event_type, freeze_now):
    with freeze_now(NOW):
        created = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        reference = created.json()["reference"]
        assert created.json()["code_url"] == CODE_URL

        page = e2e.get(f"/book/{reference}")
        assert page.status_code == 200
        assert "<svg" in page.text
        assert CODE_URL not in page.text
        assert "weixin://" not in page.text


# ---------------------------------------------------------------------------
# The double-booking race, driven through HTTP
# ---------------------------------------------------------------------------


def test_double_booking_race_through_http_exactly_one_wins(
    e2e, event_type, db_session, wechat_sent
):
    slot = next_bookable_local()
    barrier = threading.Barrier(2)

    def attempt(name: str):
        barrier.wait()
        return e2e.post(
            "/api/bookings",
            json=payload(slot, customer_name=name, customer_email=f"{name}@example.com"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "alice"), pool.submit(attempt, "bob")]
        responses = [future.result() for future in futures]

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409], [(r.status_code, r.text) for r in responses]

    live = db_session.scalars(
        select(Booking).where(
            Booking.event_type_id == "consult-30",
            Booking.slot_start == slot.astimezone(UTC),
            Booking.status.in_([BookingStatus.PENDING_PAYMENT, BookingStatus.PAID]),
        )
    ).all()
    assert len(live) == 1
    # The loser must fail before it creates a calendar hold or a WeChat order.
    assert len(wechat_sent) == 1


# ---------------------------------------------------------------------------
# Expiry without the sweeper
# ---------------------------------------------------------------------------


def test_expired_hold_frees_its_slot_with_the_sweeper_never_invoked(
    e2e, event_type, db_session, fake_calendar, freeze_now
):
    """Contract §7: the worker is hygiene, not correctness."""
    with freeze_now(NOW):
        first = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        assert first.status_code == 201, first.text
        reference = first.json()["reference"]

        row = reload_booking(db_session, reference)
        row.expires_at = NOW - timedelta(minutes=1)
        db_session.commit()

        # Nothing swept: the hold is still on the calendar as stale residue.
        assert fake_calendar.released == []
        assert any(interval.start == SLOT_UTC for interval in fake_calendar.busy)

        listed = e2e.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": LOCAL_DATE}
        )
        assert SLOT_UTC in offered_slot_starts(listed)

        second = e2e.post(
            "/api/bookings",
            json=payload(SLOT_LOCAL, customer_name="Bob", customer_email="bob@example.com"),
        )
        assert second.status_code == 201, second.text
        assert second.json()["reference"] != reference

        assert reload_booking(db_session, reference).status is BookingStatus.EXPIRED
        assert fake_calendar.released == []  # still no sweeper


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_path_releases_the_hold_and_frees_the_slot(
    e2e, event_type, db_session, fake_calendar, freeze_now
):
    with freeze_now(NOW):
        created = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        assert created.status_code == 201
        reference = created.json()["reference"]
        event_id = reload_booking(db_session, reference).calendar_event_id
        assert event_id is not None

        cancelled = e2e.post(f"/api/bookings/{reference}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert fake_calendar.released == [event_id]
        assert reload_booking(db_session, reference).status is BookingStatus.CANCELLED

        listed = e2e.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": LOCAL_DATE}
        )
        assert SLOT_UTC in offered_slot_starts(listed)

        again = e2e.post(f"/api/bookings/{reference}/cancel")
        assert again.status_code == 409


# ---------------------------------------------------------------------------
# A callback that arrives after the hold deadline
# ---------------------------------------------------------------------------


def test_callback_after_the_hold_deadline_is_still_honoured(
    e2e,
    event_type,
    db_session,
    fake_calendar,
    recording_email,
    wechat_keys,
    freeze_now,
):
    """DECISION (M7): a verified payment is honoured even after the hold deadline.

    The row is still ``pending_payment`` in the DB — nothing has swept it — so the
    partial unique index still owns the slot and honouring the money cannot
    double-book anything.  Refusing it would take ¥500 with no refund path in v1
    (§13 excludes refunds), which is strictly worse than a late confirmation.
    """
    with freeze_now(NOW):
        created = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        reference = created.json()["reference"]

        row = reload_booking(db_session, reference)
        row.expires_at = NOW - timedelta(minutes=1)
        db_session.commit()

        ack = post_signed_notify(
            e2e,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=new_transaction_id(),
        )
        assert ack.status_code == 200
        assert ack.json()["code"] == "SUCCESS"

        assert reload_booking(db_session, reference).status is BookingStatus.PAID
        assert len(fake_calendar.confirmed) == 1
        assert len(recording_email.sent) == 1


# Was `xfail` while the bug was open: the notify handler answered SUCCESS while
# `mark_paid` was a no-op on an already-`expired` row, so a real payment was
# dropped with no retry and no refund path.  Fixed by routing the settle through
# `app.services.booking.honour_late_payment`, which either honours the payment or
# raises `PaymentConflict` — and by the handler refusing to ack SUCCESS unless the
# booking really is paid.  This test is now a plain regression guard.
def test_a_success_ack_always_means_the_booking_was_marked_paid(
    e2e, event_type, db_session, fake_calendar, wechat_keys, freeze_now
):
    """The invariant: never tell WeChat SUCCESS unless the booking is actually paid.

    Reached by the normal flow: hold A expires, customer B books the same slot
    (the transactional pre-insert sweep flips A to ``expired``), then A's genuine
    signed callback arrives.  Whatever the policy for a late payment, the ACK and
    the booking state must agree — a SUCCESS with the booking still unpaid is the
    one outcome that loses money silently.
    """
    with freeze_now(NOW):
        first = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        reference = first.json()["reference"]

        row = reload_booking(db_session, reference)
        row.expires_at = NOW - timedelta(minutes=1)
        db_session.commit()

        second = e2e.post(
            "/api/bookings",
            json=payload(SLOT_LOCAL, customer_name="Bob", customer_email="bob@example.com"),
        )
        assert second.status_code == 201, second.text
        assert reload_booking(db_session, reference).status is BookingStatus.EXPIRED

        ack = post_signed_notify(
            e2e,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=new_transaction_id(),
        )

        status = reload_booking(db_session, reference).status
        assert not (ack.json()["code"] == "SUCCESS" and status is not BookingStatus.PAID), (
            f"WeChat was answered {ack.json()['code']!r} for {reference} but the booking is "
            f"{status.value}: the payment is dropped with no retry and no refund path"
        )

        # Bob already owns the slot, so we must NOT steal it back from him: the
        # late payment is refused loudly and needs a human (a refund, since v1 has
        # none).  What is forbidden is a quiet success.
        assert ack.json()["code"] == "FAIL"
        assert status is BookingStatus.EXPIRED
        assert "refund" in ack.json()["message"]

        # And the money is still on the record for whoever has to reconcile it.
        event = db_session.scalar(
            select(PaymentEvent).where(PaymentEvent.out_trade_no == reference)
        )
        assert event is not None
        assert event.outcome == "paid_conflict"


# ---------------------------------------------------------------------------
# Replay of a transaction_id after the booking is paid
# ---------------------------------------------------------------------------


def test_replayed_transaction_id_after_paid_has_no_second_side_effect(
    e2e, event_type, db_session, fake_calendar, recording_email, wechat_keys, freeze_now
):
    with freeze_now(NOW):
        created = e2e.post("/api/bookings", json=payload(SLOT_LOCAL))
        reference = created.json()["reference"]
        transaction_id = new_transaction_id()

        first = post_signed_notify(
            e2e,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=transaction_id,
        )
        assert first.json()["code"] == "SUCCESS"

        paid_at = reload_booking(db_session, reference).paid_at
        assert paid_at is not None
        assert len(recording_email.sent) == 1
        assert len(fake_calendar.confirmed) == 1
        assert db_session.scalar(select(func.count()).select_from(PaymentEvent)) == 1

        second = post_signed_notify(
            e2e,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=transaction_id,
            success_time=NOW + timedelta(minutes=5),
        )
        assert second.status_code == 200
        assert second.json()["code"] == "SUCCESS"

        assert db_session.scalar(select(func.count()).select_from(PaymentEvent)) == 1
        assert reload_booking(db_session, reference).paid_at == paid_at
        assert len(recording_email.sent) == 1
        assert len(fake_calendar.confirmed) == 1


# ---------------------------------------------------------------------------
# The other big seam: booking app → REAL relay client → REAL relay app
# ---------------------------------------------------------------------------


def test_relay_seam_real_adapter_against_the_real_relay_app(
    client,
    event_type,
    recording_email,
    wechat_gateway,
    wechat_keys,
    relay_seam,
    wire,
    freeze_now,
):
    gateway, google, relay_client = relay_seam
    wire(calendar=gateway, payments=wechat_gateway, email=recording_email)

    with freeze_now(NOW):
        listed = client.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": LOCAL_DATE}
        )
        assert listed.status_code == 200
        assert SLOT_UTC in offered_slot_starts(listed)

        created = client.post("/api/bookings", json=payload(SLOT_LOCAL))
        assert created.status_code == 201, created.text
        reference = created.json()["reference"]

        # freebusy + POST /events really went over the HMAC-signed relay contract.
        assert len(google.events) == 1
        event_id = next(iter(google.events))
        assert google.events[event_id]["summary"] == f"HOLD · Alice · {reference}"

        notify = post_signed_notify(
            client,
            wechat_keys,
            out_trade_no=reference,
            amount_fen=50000,
            transaction_id=new_transaction_id(),
        )
        assert notify.status_code == 200
        assert notify.json()["code"] == "SUCCESS"

        # confirm() rewrote the hold through PATCH /events/{id} on the relay.
        assert google.events[event_id]["summary"] == f"{event_type.title} · Alice"
        assert len(recording_email.sent) == 1

    assert relay_client.get("/healthz").json() == {"ok": True}


# ---------------------------------------------------------------------------
# The client cannot influence the amount that is charged
# ---------------------------------------------------------------------------


def test_client_supplied_amount_cannot_change_the_price_charged(
    e2e, event_type, db_session, wechat_sent, freeze_now
):
    with freeze_now(NOW):
        created = e2e.post(
            "/api/bookings",
            json=payload(SLOT_LOCAL, amount_fen=1, price_fen=1, currency="USD", total=1),
        )
        assert created.status_code == 201, created.text
        assert created.json()["amount_fen"] == 50000
        assert created.json()["currency"] == "CNY"

        # What WeChat was actually asked for, through the real adapter.
        assert wechat_sent[-1]["amount"] == {"total": 50000, "currency": "CNY"}

        row = reload_booking(db_session, created.json()["reference"])
        assert row.amount_fen == 50000
        assert row.currency == "CNY"


# ---------------------------------------------------------------------------
# A slot the grid never offered is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slot_local", "why"),
    [
        ("2026-09-21T14:07:00+08:00", "off the 15-minute grid"),
        ("2026-09-21T12:00:00+08:00", "inside the 240-minute minimum notice"),
        ("2026-11-23T14:00:00+08:00", "beyond the 60-day booking window"),
    ],
)
def test_a_slot_the_grid_never_offered_is_refused_with_422(
    e2e, event_type, db_session, freeze_now, slot_local, why
):
    with freeze_now(NOW):
        before = db_session.scalar(select(func.count()).select_from(Booking))
        response = e2e.post("/api/bookings", json=payload(slot_local))
        assert response.status_code == 422, f"{why}: {response.status_code} {response.text}"
        after = db_session.scalar(select(func.count()).select_from(Booking))
        assert after == before


# ---------------------------------------------------------------------------
# Health on both deployables
# ---------------------------------------------------------------------------


def test_healthz_on_the_booking_app_and_the_relay(client):
    booking_health = client.get("/healthz")
    assert booking_health.status_code == 200
    assert booking_health.json() == {"ok": True}

    with TestClient(relay_app) as relay_client:
        relay_health = relay_client.get("/healthz")
        assert relay_health.status_code == 200
        assert relay_health.json() == {"ok": True}
