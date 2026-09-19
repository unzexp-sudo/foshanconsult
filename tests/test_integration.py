"""Cross-module guards — integrator-owned.

Each test here covers a seam *between* two modules, which is exactly the class of
bug no single module agent can see.  They exist because the parallel build
produced three real defects:

* a slot busy only in Google Calendar (no DB row) was still bookable, because
  ``is_slot_on_grid`` cannot see the calendar and ``uq_active_slot`` only guards
  against our own bookings;
* ``generate_slots`` and ``create_booking`` disagreed about our own stale holds,
  so with the sweeper down a slot vanished from the grid while the booking path
  would still have accepted it — breaking contract §7's promise that correctness
  does not depend on the worker;
* ``tests/fakes.FakePaymentGateway`` handed back ``success_time`` as an ISO
  string where the port says ``datetime | None``.

Do not delete these.  They are the regression net for the integration.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Booking, BookingStatus
from tests.fakes import FakeCalendarGateway, FakePaymentGateway

SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_bookable_local() -> datetime:
    """14:00 Shanghai on the next weekday that clears the 4h notice window."""
    now_utc = datetime.now(UTC)
    day = (now_utc.astimezone(SHANGHAI) + timedelta(days=2)).date()
    for _ in range(14):
        if day.weekday() <= 4:
            candidate = datetime.combine(day, time(14, 0), tzinfo=SHANGHAI)
            if candidate.astimezone(UTC) > now_utc + timedelta(hours=4):
                return candidate
        day += timedelta(days=1)
    raise AssertionError("no bookable weekday found in the next two weeks")


def payload(slot_local: datetime, **overrides) -> dict:
    body = {
        "event_type_id": "consult-30",
        "slot_start": slot_local.isoformat(),
        "customer_name": "Alice",
        "customer_email": "alice@example.com",
    }
    body.update(overrides)
    return body


def offered_slots(response) -> list[datetime]:
    """Parse the API's slot starts.

    FastAPI serialises an aware-UTC datetime with a trailing ``Z``, so comparing
    raw strings against ``datetime.isoformat()`` (which yields ``+00:00``) is a
    silent mismatch.  Compare datetimes.
    """
    return [
        datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        for item in response.json()["slots"]
    ]


# ---------------------------------------------------------------------------
# Seam 1: the calendar-busy guard
# ---------------------------------------------------------------------------


def test_real_meeting_blocks_both_the_grid_and_the_booking_path(
    client, event_type, fake_calendar
):
    """A slot occupied by a genuine meeting must be refused, not merely unlisted."""
    slot_local = next_bookable_local()
    slot_utc = slot_local.astimezone(UTC)

    # 13:45–14:15 Shanghai: overlaps the 14:00 slot, owned by no booking of ours.
    fake_calendar.add_busy(
        slot_utc - timedelta(minutes=15), slot_utc + timedelta(minutes=15)
    )

    listed = client.get(
        "/api/slots",
        params={"event_type_id": "consult-30", "date": slot_local.date().isoformat()},
    )
    assert listed.status_code == 200
    offered = offered_slots(listed)
    assert slot_utc not in offered

    created = client.post("/api/bookings", json=payload(slot_local))
    assert created.status_code == 422, created.text


def test_free_slot_is_both_listed_and_bookable(client, event_type):
    """The control: with nothing busy, the same slot must be offered and accepted."""
    slot_local = next_bookable_local()
    slot_utc = slot_local.astimezone(UTC)

    listed = client.get(
        "/api/slots",
        params={"event_type_id": "consult-30", "date": slot_local.date().isoformat()},
    )
    offered = offered_slots(listed)
    assert slot_utc in offered

    created = client.post("/api/bookings", json=payload(slot_local))
    assert created.status_code == 201, created.text
    assert created.json()["amount_fen"] == 50000


# ---------------------------------------------------------------------------
# Seam 2: availability and the booking path must agree when the sweeper is down
# ---------------------------------------------------------------------------


def test_expired_hold_is_offered_and_rebookable_without_the_sweeper(
    client, event_type, db_session, fake_calendar
):
    """The sweeper never runs in this test — on purpose.

    An expired hold whose calendar event is still sitting there must not hide the
    slot from the grid, and must not stop the slot being booked again.  This is
    contract §7: the worker is hygiene, not correctness.
    """
    slot_local = next_bookable_local()
    slot_utc = slot_local.astimezone(UTC)

    first = client.post("/api/bookings", json=payload(slot_local))
    assert first.status_code == 201, first.text
    reference = first.json()["reference"]

    # Expire it in the DB only.  The calendar hold and its busy interval stay put,
    # exactly as they would if the sweeper were dead.
    row = db_session.scalar(select(Booking).where(Booking.reference == reference))
    assert row is not None and row.calendar_event_id is not None
    row.status = BookingStatus.EXPIRED
    db_session.commit()

    assert fake_calendar.released == []  # proof: nothing released the hold
    assert any(
        interval.start == slot_utc for interval in fake_calendar.busy
    ), "the stale hold should still be on the calendar"

    listed = client.get(
        "/api/slots",
        params={"event_type_id": "consult-30", "date": slot_local.date().isoformat()},
    )
    offered = offered_slots(listed)
    assert slot_utc in offered, "the expired hold must free its slot"

    again = client.post("/api/bookings", json=payload(slot_local))
    assert again.status_code == 201, again.text
    assert again.json()["reference"] != reference


def test_live_hold_still_hides_its_slot(client, event_type, fake_calendar):
    """The counterpart: a hold that has NOT expired must still block."""
    slot_local = next_bookable_local()
    slot_utc = slot_local.astimezone(UTC)

    first = client.post("/api/bookings", json=payload(slot_local))
    assert first.status_code == 201

    listed = client.get(
        "/api/slots",
        params={"event_type_id": "consult-30", "date": slot_local.date().isoformat()},
    )
    offered = offered_slots(listed)
    assert slot_utc not in offered

    second = client.post("/api/bookings", json=payload(slot_local))
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# Seam 3: the payment fake must obey the port's types
# ---------------------------------------------------------------------------


def test_fake_payment_gateway_returns_a_datetime_success_time():
    gateway = FakePaymentGateway()
    headers, body = gateway.build_notify(out_trade_no="BKABC123", amount_fen=50000)

    notification = gateway.parse_notification(headers, body)

    assert notification.amount_fen == 50000
    assert isinstance(notification.success_time, datetime), (
        "the port declares success_time as datetime | None; a str leaks into the "
        "notify handler and into Booking.paid_at"
    )


def test_fake_payment_gateway_fails_closed_on_a_bad_signature():
    from app.ports.payments import PaymentSignatureError

    gateway = FakePaymentGateway()
    _, body = gateway.build_notify(out_trade_no="BKABC123", amount_fen=50000)

    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification({"X-Fake-Signature": "nope"}, body)


# ---------------------------------------------------------------------------
# Seam 4: the calendar fakes agree on idempotent release
# ---------------------------------------------------------------------------


def test_both_calendar_fakes_release_idempotently():
    from app.adapters.calendar_fake import FakeCalendarGateway as ProdFake

    for gateway in (FakeCalendarGateway(), ProdFake()):
        gateway.release("never-existed")  # must not raise
        gateway.release("never-existed")


# ---------------------------------------------------------------------------
# Seam 5: a verified payment that arrives after the hold died
#
# Found by M7's end-to-end pass.  The notify handler used to answer SUCCESS
# unconditionally while `mark_paid` was a no-op on an already-`expired` row, so a
# real payment was dropped: WeChat never retried, the audit row said "paid", and
# nobody was told.  These pin both halves of the policy.
# ---------------------------------------------------------------------------


def _expire(db_session, reference: str, *, release_hold: bool = False) -> Booking:
    row = db_session.scalar(select(Booking).where(Booking.reference == reference))
    assert row is not None
    row.status = BookingStatus.EXPIRED
    if release_hold:
        # What a successful sweep leaves behind: no hold, no calendar event.
        row.calendar_event_id = None
    db_session.commit()
    return row


def _notify(client, fake_payments, db_session, reference: str):
    headers, body = fake_payments.build_notify(out_trade_no=reference, amount_fen=50000)
    response = client.post("/api/payments/wechat/notify", content=body, headers=headers)
    # The request committed in its own session, so drop this session's identity map
    # before asserting — otherwise we read the pre-request object back.
    db_session.expire_all()
    return response


def test_late_payment_is_honoured_when_the_slot_is_still_free(
    client, event_type, db_session, fake_calendar, fake_payments, recording_email, monkeypatch
):
    """Nobody took the slot, so the customer who paid gets their call."""
    from app import deps as app_deps

    # app.tasks calls app.deps.get_* directly, so dependency_overrides does not
    # reach the background task — patch the accessors themselves.
    monkeypatch.setattr(app_deps, "get_calendar_gateway", lambda: fake_calendar)
    monkeypatch.setattr(app_deps, "get_email_sender", lambda: recording_email)

    slot_local = next_bookable_local()
    created = client.post("/api/bookings", json=payload(slot_local))
    assert created.status_code == 201, created.text
    reference = created.json()["reference"]

    stale = _expire(db_session, reference)
    assert stale.calendar_event_id is not None  # the hold was never swept

    ack = _notify(client, fake_payments, db_session, reference)

    assert ack.status_code == 200
    assert ack.json()["code"] == "SUCCESS", ack.text

    settled = db_session.scalar(select(Booking).where(Booking.reference == reference))
    assert settled is not None
    assert settled.status is BookingStatus.PAID, "a verified payment must not be dropped"
    assert settled.provider_transaction_id is not None
    assert settled.paid_at is not None

    # The owner must actually find out about the call.
    assert len(fake_calendar.confirmed) == 1
    assert len(recording_email.sent) == 1


def test_late_payment_after_the_hold_was_released_still_creates_the_event(
    client, event_type, db_session, fake_calendar, fake_payments, recording_email, monkeypatch
):
    """The deeper half of the bug: the sweep had already released the hold.

    `calendar_event_id IS NULL` used to mean both "already finalised" and "never
    held", so this case produced no calendar event and no email at all.
    """
    from app import deps as app_deps

    monkeypatch.setattr(app_deps, "get_calendar_gateway", lambda: fake_calendar)
    monkeypatch.setattr(app_deps, "get_email_sender", lambda: recording_email)

    slot_local = next_bookable_local()
    created = client.post("/api/bookings", json=payload(slot_local))
    reference = created.json()["reference"]

    _expire(db_session, reference, release_hold=True)
    fake_calendar.confirmed.clear()

    ack = _notify(client, fake_payments, db_session, reference)
    assert ack.json()["code"] == "SUCCESS", ack.text

    settled = db_session.scalar(select(Booking).where(Booking.reference == reference))
    assert settled is not None and settled.status is BookingStatus.PAID

    # A fresh event had to be created, because there was no hold left to confirm.
    assert settled.calendar_event_id is not None
    assert len(fake_calendar.confirmed) == 1
    assert len(recording_email.sent) == 1, "the customer must be told their booking is real"


def test_late_payment_is_refused_loudly_when_the_slot_was_resold(
    client, event_type, db_session, fake_calendar, fake_payments
):
    """We do not steal the slot back from the second customer, and we do not lie."""
    from app.models import PaymentEvent

    slot_local = next_bookable_local()
    first = client.post("/api/bookings", json=payload(slot_local))
    reference = first.json()["reference"]

    _expire(db_session, reference)
    second = client.post(
        "/api/bookings",
        json=payload(slot_local, customer_name="Bob", customer_email="bob@example.com"),
    )
    assert second.status_code == 201, second.text

    ack = _notify(client, fake_payments, db_session, reference)

    assert ack.json()["code"] == "FAIL"
    assert "refund" in ack.json()["message"]

    settled = db_session.scalar(select(Booking).where(Booking.reference == reference))
    assert settled is not None and settled.status is BookingStatus.EXPIRED

    # The money is on the record for whoever has to reconcile it.
    event = db_session.scalar(
        select(PaymentEvent).where(PaymentEvent.out_trade_no == reference)
    )
    assert event is not None and event.outcome == "paid_conflict"


# ---------------------------------------------------------------------------
# Seam 6: a live hold must block across event types
# ---------------------------------------------------------------------------


def test_a_live_hold_of_a_different_event_type_still_blocks(
    db_session, event_type, fake_calendar, fake_payments
):
    """Two 1-1 calls cannot overlap, even for different offerings.

    `owned_intervals` used to ignore *any* interval we owned, so a live hold of
    event type X did not block event type Y at the same time — a latent
    double-booking hole the moment a second event type exists.
    """
    from app.models import AvailabilityRule, EventType
    from app.services.availability import generate_slots
    from app.services.booking import SlotNotBookable, create_booking

    other = EventType(
        id="deepdive-60",
        title="60 分钟深度诊断",
        description="",
        duration_minutes=60,
        price_fen=100000,
        currency="CNY",
        min_notice_minutes=240,
        max_days_ahead=60,
        timezone="Asia/Shanghai",
        active=True,
        availability_rules=[
            AvailabilityRule(weekday=day, start_local=time(9, 0), end_local=time(18, 0))
            for day in range(5)
        ],
    )
    db_session.add(other)
    db_session.commit()

    slot_local = next_bookable_local()
    slot_utc = slot_local.astimezone(UTC)

    held = create_booking(
        db_session,
        event_type_id="consult-30",
        slot_start=slot_utc,
        customer_name="Alice",
        customer_email="alice@example.com",
        calendar=fake_calendar,
        payments=fake_payments,
    )
    assert held.status is BookingStatus.PENDING_PAYMENT

    offered = [
        slot.start
        for slot in generate_slots(
            db_session, other, slot_local.date(), calendar=fake_calendar
        )
    ]
    assert slot_utc not in offered, "a live hold must block the grid for every event type"

    with pytest.raises(SlotNotBookable):
        create_booking(
            db_session,
            event_type_id="deepdive-60",
            slot_start=slot_utc,
            customer_name="Bob",
            customer_email="bob@example.com",
            calendar=fake_calendar,
            payments=fake_payments,
        )
