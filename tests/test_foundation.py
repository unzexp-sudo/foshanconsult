"""Foundation acceptance tests.

These assert the invariants every other module leans on.  If one of these fails,
nothing built on top of it can be trusted — fix the foundation before touching a
module.

Run: ``uv run pytest tests/test_foundation.py``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import utcnow
from app.models import Booking, BookingStatus, EventType
from app.ports.calendar import CalendarGateway
from app.ports.email import EmailSender
from app.ports.payments import PaymentGateway
from tests.fakes import FakeCalendarGateway, FakePaymentGateway, RecordingEmailSender


def _booking(db, event_type, slot_start: datetime, status: BookingStatus) -> Booking:
    now = utcnow()
    row = Booking(
        id=uuid.uuid4().hex,
        reference=f"BK{uuid.uuid4().hex[:6].upper()}",
        event_type_id=event_type.id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=event_type.duration_minutes),
        status=status,
        expires_at=now + timedelta(minutes=10),
        amount_fen=event_type.price_fen,
        currency="CNY",
        customer_name="Test",
        customer_email="test@example.com",
    )
    db.add(row)
    return row


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_seed_creates_consult_30(db_session):
    from app.seed import CONSULT_ID, seed

    event_type = seed(db_session)
    db_session.commit()

    assert event_type.id == CONSULT_ID
    assert event_type.price_fen == 50000  # ¥500 in 分 — never a float
    assert event_type.currency == "CNY"
    assert event_type.duration_minutes == 30
    assert event_type.min_notice_minutes == 240
    assert event_type.max_days_ahead == 60
    assert event_type.timezone == "Asia/Shanghai"
    assert sorted(rule.weekday for rule in event_type.availability_rules) == [0, 1, 2, 3, 4]


def test_seed_is_idempotent(db_session):
    from app.seed import seed

    seed(db_session)
    seed(db_session)
    db_session.commit()

    assert db_session.query(EventType).count() == 1


# ---------------------------------------------------------------------------
# The double-booking guard — the single most important schema detail
# ---------------------------------------------------------------------------


def test_partial_unique_index_exists():
    from app.db import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_active_slot'")
        ).fetchall()
    assert rows, "uq_active_slot is missing from the schema"


def test_second_live_booking_for_same_slot_is_rejected(db_session, event_type):
    slot = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)

    _booking(db_session, event_type, slot, BookingStatus.PENDING_PAYMENT)
    db_session.commit()

    _booking(db_session, event_type, slot, BookingStatus.PENDING_PAYMENT)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_paid_also_occupies_the_slot(db_session, event_type):
    slot = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)

    _booking(db_session, event_type, slot, BookingStatus.PAID)
    db_session.commit()

    _booking(db_session, event_type, slot, BookingStatus.PAID)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("status", [BookingStatus.EXPIRED, BookingStatus.CANCELLED])
def test_dead_bookings_do_not_occupy_the_slot(db_session, event_type, status):
    """The predicate must be evaluated against the *value*, not the member name.

    If the enum ever stores ``PENDING_PAYMENT`` instead of ``pending_payment``, the
    predicate silently stops matching and this test is what catches it.
    """
    slot = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)

    _booking(db_session, event_type, slot, status)
    _booking(db_session, event_type, slot, BookingStatus.PENDING_PAYMENT)
    db_session.commit()  # must not raise

    assert db_session.query(Booking).count() == 2


def test_status_is_stored_as_the_enum_value(db_session, event_type):
    slot = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    _booking(db_session, event_type, slot, BookingStatus.PENDING_PAYMENT)
    db_session.commit()

    raw = db_session.execute(text("SELECT status FROM bookings")).scalar_one()
    assert raw == "pending_payment"


# ---------------------------------------------------------------------------
# Expiry helper
# ---------------------------------------------------------------------------


def test_is_expired_uses_lazy_expiry(db_session, event_type):
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    slot = now + timedelta(hours=6)

    row = _booking(db_session, event_type, slot, BookingStatus.PENDING_PAYMENT)
    row.expires_at = now - timedelta(seconds=1)
    db_session.commit()

    assert row.is_expired(now) is True
    assert row.is_expired(now - timedelta(minutes=5)) is False


def test_paid_booking_is_never_expired(db_session, event_type):
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    row = _booking(db_session, event_type, now, BookingStatus.PAID)
    row.expires_at = now - timedelta(days=1)
    db_session.commit()

    assert row.is_expired(now) is False


# ---------------------------------------------------------------------------
# Config + ports
# ---------------------------------------------------------------------------


def test_notify_url_is_derived():
    assert settings.wechat_notify_url == (
        f"{settings.public_base_url.rstrip('/')}/api/payments/wechat/notify"
    )


def test_fakes_satisfy_their_ports():
    assert isinstance(FakeCalendarGateway(), CalendarGateway)
    assert isinstance(FakePaymentGateway(), PaymentGateway)
    assert isinstance(RecordingEmailSender(), EmailSender)


def test_booking_uses_aware_utc():
    from app.db import utcnow as _utcnow

    assert _utcnow().tzinfo is not None
