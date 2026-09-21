"""Booking service and router tests (M1, contract §14.2 / §11).

The important one is :func:`test_two_concurrent_creates_exactly_one_wins`: it
drives the real partial unique index on the real SQLite file DB with two
independent sessions, rather than trusting a mock.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import SessionLocal
from app.models import Booking, BookingStatus, EventType
from app.services import booking as booking_service
from app.services.booking import SlotTaken, create_booking
from tests.fakes import FakePaymentGateway

# Monday 2026-09-21.  NOW is Mon 09:00 Shanghai; consult-30 needs 4h notice.
NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
SLOT_LOCAL = "2026-09-21T14:00:00+08:00"
SLOT_UTC = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
SLOT2_LOCAL = "2026-09-21T15:00:00+08:00"
SLOT2_UTC = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


def payload(slot_local: str = SLOT_LOCAL, **overrides) -> dict:
    body = {
        "event_type_id": "consult-30",
        "slot_start": slot_local,
        "customer_name": "Alice",
        "customer_email": "alice@example.com",
    }
    body.update(overrides)
    return body


def add_booking(
    db,
    event_type: EventType,
    slot_start: datetime,
    *,
    status: BookingStatus = BookingStatus.PENDING_PAYMENT,
    expires_at: datetime | None = None,
    reference: str | None = None,
    customer_name: str = "Direct",
    created_at: datetime | None = None,
) -> Booking:
    booking = Booking(
        id=uuid.uuid4().hex,
        reference=reference or f"BK{uuid.uuid4().hex[:6].upper()}",
        event_type_id=event_type.id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=event_type.duration_minutes),
        status=status,
        expires_at=expires_at or NOW + timedelta(minutes=10),
        amount_fen=event_type.price_fen,
        currency="CNY",
        customer_name=customer_name,
        customer_email="direct@example.com",
    )
    if created_at is not None:
        booking.created_at = created_at
    db.add(booking)
    db.commit()
    return booking


def load(db, reference: str) -> Booking:
    db.expire_all()
    row = db.scalar(select(Booking).where(Booking.reference == reference))
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Service: the double-booking guard, for real
# ---------------------------------------------------------------------------


def test_two_concurrent_creates_exactly_one_wins(event_type, fake_calendar, fake_payments):
    """Two threads, two real sessions, the real uq_active_slot index."""
    barrier = threading.Barrier(2)

    def attempt(name: str):
        barrier.wait()
        for try_index in range(2):  # one retry on a SQLite lock, never weaker
            db = SessionLocal()
            try:
                booking = create_booking(
                    db,
                    event_type_id=event_type.id,
                    slot_start=SLOT_UTC,
                    customer_name=name,
                    customer_email=f"{name}@example.com",
                    calendar=fake_calendar,
                    payments=fake_payments,
                    now=NOW,
                )
                return ("created", booking.reference)
            except SlotTaken as exc:
                return ("taken", str(exc))
            except OperationalError:
                db.rollback()
                if try_index == 1:
                    raise
            finally:
                db.close()
        raise AssertionError("unreachable")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "alice"), pool.submit(attempt, "bob")]
        outcomes = [future.result() for future in futures]

    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["created", "taken"], outcomes

    db = SessionLocal()
    try:
        live = db.scalars(
            select(Booking).where(
                Booking.event_type_id == event_type.id,
                Booking.slot_start == SLOT_UTC,
                Booking.status.in_([BookingStatus.PENDING_PAYMENT, BookingStatus.PAID]),
            )
        ).all()
    finally:
        db.close()
    assert len(live) == 1


# ---------------------------------------------------------------------------
# Service: expiry frees a slot
# ---------------------------------------------------------------------------


def test_expired_hold_frees_the_slot_again(
    client, event_type, fake_calendar, db_session, freeze_now
):
    with freeze_now(NOW):
        first = client.post("/api/bookings", json=payload())
        assert first.status_code == 201
        reference = first.json()["reference"]

        # Lazy-expire the hold and simulate the sweeper releasing the calendar.
        row = load(db_session, reference)
        row.expires_at = NOW - timedelta(minutes=1)
        db_session.commit()
        if row.calendar_event_id:
            fake_calendar.release(row.calendar_event_id)

        listed = client.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": "2026-09-21"}
        )
        offered = [datetime.fromisoformat(s["start"]) for s in listed.json()["slots"]]
        assert SLOT_UTC in offered

        second = client.post("/api/bookings", json=payload())
        assert second.status_code == 201
        assert second.json()["reference"] != reference

    # The stale row was flipped by the transactional pre-insert sweep.
    assert load(db_session, reference).status is BookingStatus.EXPIRED


# ---------------------------------------------------------------------------
# Service: failures never leave a hold without an order
# ---------------------------------------------------------------------------


def test_payment_failure_rolls_back_and_releases_the_hold(
    event_type, fake_calendar, db_session
):
    failing = FakePaymentGateway(fail_create=True)
    with pytest.raises(RuntimeError):
        create_booking(
            db_session,
            event_type_id=event_type.id,
            slot_start=SLOT_UTC,
            customer_name="Alice",
            customer_email="alice@example.com",
            calendar=fake_calendar,
            payments=failing,
            now=NOW,
        )

    assert fake_calendar.events == {}  # hold released, not left dangling
    assert fake_calendar.released
    assert db_session.query(Booking).count() == 0


def test_mark_paid_is_idempotent_and_does_not_touch_calendar(event_type, db_session, fake_calendar):
    row = add_booking(db_session, event_type, SLOT_UTC)
    booking_service.mark_paid(db_session, row, transaction_id="tx-1", paid_at=NOW)
    db_session.commit()

    first_paid_at = row.paid_at
    booking_service.mark_paid(
        db_session, row, transaction_id="tx-2", paid_at=NOW + timedelta(hours=1)
    )
    db_session.commit()

    assert row.status is BookingStatus.PAID
    assert row.provider_transaction_id == "tx-1"  # unchanged
    assert row.paid_at == first_paid_at
    assert fake_calendar.confirmed == []  # finalize is M6's job


# ---------------------------------------------------------------------------
# Router: slots + create
# ---------------------------------------------------------------------------


def test_held_slot_is_not_offered_and_post_returns_409(client, event_type, freeze_now):
    with freeze_now(NOW):
        first = client.post("/api/bookings", json=payload())
        assert first.status_code == 201

        listed = client.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": "2026-09-21"}
        )
        assert listed.status_code == 200
        offered = [datetime.fromisoformat(s["start"]) for s in listed.json()["slots"]]
        assert SLOT_UTC not in offered

        second = client.post("/api/bookings", json=payload())
        assert second.status_code == 409


def test_create_rejects_off_grid_slot_with_422(client, event_type, freeze_now):
    with freeze_now(NOW):
        response = client.post("/api/bookings", json=payload("2026-09-21T14:07:00+08:00"))
    assert response.status_code == 422


def test_create_rejects_unknown_event_type_with_404(client, freeze_now):
    with freeze_now(NOW):
        response = client.post(
            "/api/bookings", json=payload(event_type_id="does-not-exist")
        )
    assert response.status_code == 404


def test_slots_endpoint_404s_unknown_event_type_and_422s_bad_date(client, event_type):
    assert (
        client.get("/api/slots", params={"event_type_id": "nope", "date": "2026-09-21"}).status_code
        == 404
    )
    assert (
        client.get(
            "/api/slots", params={"event_type_id": "consult-30", "date": "21-09-2026"}
        ).status_code
        == 422
    )


def test_event_types_lists_active_only(client, event_type, db_session):
    inactive = EventType(
        id="hidden",
        title="hidden",
        description="",
        duration_minutes=30,
        price_fen=1,
        currency="CNY",
        timezone="Asia/Shanghai",
        active=False,
    )
    db_session.add(inactive)
    db_session.commit()

    body = client.get("/api/event-types").json()
    assert [item["id"] for item in body] == ["consult-30"]


# ---------------------------------------------------------------------------
# Router: the client cannot influence the amount
# ---------------------------------------------------------------------------


def test_client_supplied_amount_is_ignored(client, event_type, db_session, freeze_now):
    with freeze_now(NOW):
        response = client.post(
            "/api/bookings",
            json=payload(
                SLOT2_LOCAL,
                amount_fen=1,
                price_fen=1,
                currency="USD",
                total=1,
            ),
        )
    assert response.status_code == 201
    body = response.json()
    assert body["amount_fen"] == 50000
    assert body["currency"] == "CNY"

    stored = load(db_session, body["reference"])
    assert stored.amount_fen == 50000
    assert stored.currency == "CNY"
    assert stored.slot_start.replace(tzinfo=UTC) == SLOT2_UTC


# ---------------------------------------------------------------------------
# Router: status + cancel
# ---------------------------------------------------------------------------


def test_get_unknown_booking_returns_404(client):
    assert client.get("/api/bookings/BKNOPE00").status_code == 404


def test_cancel_works_while_pending_and_is_refused_once_paid(
    client, event_type, db_session, freeze_now
):
    with freeze_now(NOW):
        first = client.post("/api/bookings", json=payload())
        reference = first.json()["reference"]

        cancelled = client.post(f"/api/bookings/{reference}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        second = client.post("/api/bookings", json=payload(SLOT2_LOCAL))
        assert second.status_code == 201
        paid_reference = second.json()["reference"]

    row = load(db_session, paid_reference)
    booking_service.mark_paid(db_session, row, transaction_id="tx-paid", paid_at=NOW)
    db_session.commit()

    refused = client.post(f"/api/bookings/{paid_reference}/cancel")
    assert refused.status_code == 409
    assert load(db_session, paid_reference).status is BookingStatus.PAID


def test_cancel_unknown_booking_returns_404(client, event_type):
    assert client.post("/api/bookings/BKNOPE00/cancel").status_code == 404


def test_get_booking_returns_status(client, event_type, freeze_now):
    with freeze_now(NOW):
        created = client.post("/api/bookings", json=payload())
        reference = created.json()["reference"]
        fetched = client.get(f"/api/bookings/{reference}")

    assert fetched.status_code == 200
    body = fetched.json()
    assert body["reference"] == reference
    assert body["status"] == "pending_payment"
    assert body["amount_fen"] == 50000
    assert body["event_type_id"] == "consult-30"


# ---------------------------------------------------------------------------
# Router: admin
# ---------------------------------------------------------------------------


def test_admin_requires_the_right_token(client, event_type, db_session):
    add_booking(
        db_session, event_type, SLOT_UTC, customer_name="Older", created_at=NOW - timedelta(hours=1)
    )
    add_booking(
        db_session, event_type, SLOT2_UTC, customer_name="Newer", created_at=NOW
    )

    assert client.get("/api/admin/bookings").status_code == 401
    assert (
        client.get("/api/admin/bookings", headers={"X-Admin-Token": "wrong"}).status_code == 401
    )

    ok = client.get("/api/admin/bookings", headers={"X-Admin-Token": settings.secret_key})
    assert ok.status_code == 200
    body = ok.json()
    assert [item["customer_name"] for item in body] == ["Newer", "Older"]  # newest first
    assert body[0]["amount_fen"] == 50000


# ---------------------------------------------------------------------------
# Integrator addition (2026-09-19): the calendar-busy guard
#
# `is_slot_on_grid` cannot see the calendar and `uq_active_slot` only guards
# against *our own* bookings, so without a busy check a slot the grid never
# offered was still bookable by anyone who POSTed it.
# ---------------------------------------------------------------------------


def test_slot_busy_only_in_the_calendar_is_refused(
    db_session, event_type, fake_calendar, fake_payments
):
    """A real meeting that exists only in Google Calendar must not be bookable."""
    fake_calendar.add_busy(SLOT_UTC, SLOT_UTC + timedelta(minutes=30))

    with pytest.raises(booking_service.SlotNotBookable):
        create_booking(
            db_session,
            event_type_id="consult-30",
            slot_start=SLOT_UTC,
            customer_name="Mallory",
            customer_email="mallory@example.com",
            calendar=fake_calendar,
            payments=fake_payments,
            now=NOW,
        )

    assert db_session.scalar(select(Booking)) is None
    assert fake_payments.charges == []  # never charged for a slot we refused
    assert fake_calendar.events == {}  # and never held it


def test_busy_check_is_over_the_endpoint_the_calendar_reports(
    client, event_type, fake_calendar, freeze_now
):
    """The same refusal must come out of POST /api/bookings as a 422, not a 500.

    The clock MUST be frozen.  This test goes through the HTTP surface, which has no
    ``now`` seam, while ``SLOT_LOCAL``/``SLOT_UTC`` are fixed instants — so without a
    freeze the 4-hour minimum-notice window closes at 10:00 Shanghai on 2026-09-21 and
    the request is refused for being *too soon* instead of for clashing with the
    calendar.  Both are 422, so the status assertion alone would not have noticed; the
    ``"calendar"`` assertion below is what catches it.  This test did in fact start
    failing at 10:00 Shanghai on 2026-09-21 for exactly that reason.
    """
    fake_calendar.add_busy(SLOT_UTC, SLOT_UTC + timedelta(minutes=30))

    with freeze_now(NOW):
        response = client.post("/api/bookings", json=payload())

    assert response.status_code == 422
    assert "calendar" in response.json()["detail"].lower()


def test_our_own_stale_hold_does_not_permanently_burn_the_slot(
    db_session, event_type, fake_calendar, fake_payments
):
    """A dead sweeper leaves our own calendar hold behind after the hold expires.

    That residue must not block the slot forever — the DB's lazy expiry is the
    authority on intervals we created, and only on those.
    """
    stale = add_booking(db_session, event_type, SLOT_UTC, status=BookingStatus.EXPIRED)
    stale.calendar_event_id = "evt-stale"
    db_session.commit()

    # The sweeper never ran, so the interval is still on the calendar.
    fake_calendar.add_busy(SLOT_UTC, SLOT_UTC + timedelta(minutes=30))

    booking = create_booking(
        db_session,
        event_type_id="consult-30",
        slot_start=SLOT_UTC,
        customer_name="Alice",
        customer_email="alice@example.com",
        calendar=fake_calendar,
        payments=fake_payments,
        now=NOW,
    )

    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.calendar_event_id is not None
