"""Expiry, sweeper and email tests (M6, contract §7 / §10 / §14.3).

No network, no Redis, no Celery broker: every task is called synchronously and every
gateway is the shared fake from ``tests/fakes.py``.

The load-bearing test is
:func:`test_expired_slot_is_bookable_again_without_the_sweeper`: it proves §7's claim
that slot freeing does not depend on the worker.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header

import pytest

from app import deps, tasks
from app.adapters.email_console import ConsoleEmailSender
from app.adapters.email_smtp import SmtpEmailSender
from app.config import settings
from app.models import Booking, BookingStatus
from app.services import booking as booking_service
from app.services import sweeper
from app.services.availability import generate_slots
from app.services.booking import booking_summary, create_booking

# Monday 2026-09-21.  NOW is Mon 09:00 Shanghai; consult-30 needs 4h notice.
NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
SLOT_UTC = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)  # 14:00 Shanghai
SLOT2_UTC = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)  # 15:00 Shanghai
LOCAL_DATE = date(2026, 9, 21)


@pytest.fixture
def wired_deps(monkeypatch, fake_calendar, fake_payments, recording_email):
    """Point ``app.deps`` at the shared fakes.

    ``app.tasks`` resolves its gateways through ``app.deps.get_*`` at call time, so
    swapping the module attributes is what makes the fakes observable here.
    """
    monkeypatch.setattr(deps, "get_calendar_gateway", lambda: fake_calendar)
    monkeypatch.setattr(deps, "get_payment_gateway", lambda: fake_payments)
    monkeypatch.setattr(deps, "get_email_sender", lambda: recording_email)


def make_hold(
    db,
    event_type,
    calendar,
    payments,
    *,
    slot_start: datetime = SLOT_UTC,
    name: str = "Alice",
    email: str = "alice@example.com",
) -> Booking:
    """Create a real hold through the booking service (calendar hold + WeChat order)."""
    return create_booking(
        db,
        event_type_id=event_type.id,
        slot_start=slot_start,
        customer_name=name,
        customer_email=email,
        calendar=calendar,
        payments=payments,
        now=NOW,
    )


def load(db, booking_id: str) -> Booking:
    db.expire_all()
    row = db.get(Booking, booking_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# §7 — correctness does not depend on the worker (the important test)
# ---------------------------------------------------------------------------


def test_expired_slot_is_bookable_again_without_the_sweeper(
    event_type, db_session, fake_calendar, fake_payments
):
    """The sweeper never runs; the slot still frees and can be booked again.

    The calendar hold is deliberately left in place as stale residue — that is
    exactly the state a dead sweeper leaves behind, and §7.3 promises it costs
    nothing.  The DB's lazy expiry plus the transactional pre-insert sweep are what
    free the slot.
    """
    first = make_hold(db_session, event_type, fake_calendar, fake_payments)
    hold_event_id = first.calendar_event_id
    assert hold_event_id is not None
    assert hold_event_id in fake_calendar.events  # a real hold exists

    # The hold times out in the DB only.  No sweeper, so the calendar is untouched.
    first.expires_at = NOW - timedelta(minutes=1)
    db_session.commit()

    assert fake_calendar.released == []  # sweeper was never invoked
    assert fake_payments.closed == []  # ...neither of its two jobs
    assert hold_event_id in fake_calendar.events  # stale hold is the only residue

    second = make_hold(
        db_session, event_type, fake_calendar, fake_payments, name="Bob", email="bob@example.com"
    )

    assert second.reference != first.reference
    assert second.status is BookingStatus.PENDING_PAYMENT
    assert load(db_session, first.id).status is BookingStatus.EXPIRED  # pre-insert sweep
    assert fake_calendar.released == []  # and still no sweeper


def test_availability_service_offers_slot_for_expired_hold_without_the_sweeper(
    event_type, db_session, fake_calendar
):
    """§7.1: the availability grid applies lazy expiry on its own.

    An expired ``pending_payment`` row must not occupy its slot even though no sweeper
    has ever run.  (A real hold also leaves a calendar busy interval behind; §7.3
    accepts that stale interval as the *only* residue, which is why the end-to-end
    guarantee is asserted through the booking path above.)
    """
    db_session.add(
        Booking(
            id=uuid.uuid4().hex,
            reference="BKEXPIRY1",
            event_type_id=event_type.id,
            slot_start=SLOT_UTC,
            slot_end=SLOT_UTC + timedelta(minutes=event_type.duration_minutes),
            status=BookingStatus.PENDING_PAYMENT,
            expires_at=NOW - timedelta(minutes=1),
            amount_fen=event_type.price_fen,
            currency="CNY",
            customer_name="Alice",
            customer_email="alice@example.com",
        )
    )
    db_session.commit()

    offered = [
        slot.start
        for slot in generate_slots(
            db_session, event_type, LOCAL_DATE, calendar=fake_calendar, now=NOW
        )
    ]

    assert SLOT_UTC in offered
    assert fake_calendar.released == []  # never swept


# ---------------------------------------------------------------------------
# release_expired_holds
# ---------------------------------------------------------------------------


def _expire(db, booking: Booking) -> None:
    booking.status = BookingStatus.EXPIRED
    booking.expires_at = NOW - timedelta(minutes=1)
    db.commit()


def test_release_expired_holds_releases_calendar_and_closes_order(
    event_type, db_session, fake_calendar, fake_payments, wired_deps
):
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    event_id = booking.calendar_event_id
    out_trade_no = booking.out_trade_no
    _expire(db_session, booking)

    assert tasks.release_expired_holds() == 1

    assert fake_calendar.released == [event_id]
    assert fake_payments.closed == [out_trade_no]
    assert load(db_session, booking.id).calendar_event_id is None  # marker cleared


def test_release_expired_holds_twice_releases_once(
    event_type, db_session, fake_calendar, fake_payments, wired_deps
):
    """The ``calendar_event_id = None`` marker is what makes the sweep idempotent."""
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    event_id = booking.calendar_event_id
    _expire(db_session, booking)

    assert tasks.release_expired_holds() == 1
    assert tasks.release_expired_holds() == 0

    assert fake_calendar.released == [event_id]  # exactly once
    assert fake_payments.closed == [booking.out_trade_no]  # exactly once


def test_release_expired_holds_leaves_paid_and_pending_rows_alone(
    event_type, db_session, fake_calendar, fake_payments, wired_deps
):
    paid = make_hold(db_session, event_type, fake_calendar, fake_payments)
    pending = make_hold(
        db_session, event_type, fake_calendar, fake_payments, slot_start=SLOT2_UTC
    )
    paid.status = BookingStatus.PAID
    paid_event = paid.calendar_event_id
    pending_event = pending.calendar_event_id
    db_session.commit()

    assert tasks.release_expired_holds() == 0

    assert fake_calendar.released == []
    assert fake_payments.closed == []
    assert load(db_session, paid.id).calendar_event_id == paid_event
    assert load(db_session, pending.id).calendar_event_id == pending_event


# ---------------------------------------------------------------------------
# finalize_paid_booking
# ---------------------------------------------------------------------------


def test_finalize_paid_booking_confirms_and_sends_one_email(
    event_type, db_session, fake_calendar, fake_payments, recording_email, wired_deps
):
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    event_id = booking.calendar_event_id
    booking_service.mark_paid(db_session, booking, transaction_id="tx-1", paid_at=NOW)
    db_session.commit()

    tasks.finalize_paid_booking(booking.id)

    assert fake_calendar.confirmed == [event_id]
    event = fake_calendar.events[event_id]
    assert event["status"] == "confirmed"
    assert event["summary"] == f"{event_type.title} · Alice"
    assert event["description"] == booking_summary(load(db_session, booking.id))

    assert len(recording_email.sent) == 1
    message = recording_email.sent[0]
    assert message["to"] == "alice@example.com"
    assert booking.reference in message["subject"]
    assert booking.reference in message["body"]
    assert "2026-09-21 14:00" in message["body"]  # slot time in Asia/Shanghai
    assert "¥500.00" in message["body"]
    assert f"{settings.public_base_url}/book/{booking.reference}" in message["body"]

    # The finalized marker is now its own column (integrator addition, contract
    # §15).  `calendar_event_id` deliberately survives: it is a calendar fact, and
    # clearing it here is what used to make "already finalised" and "never held"
    # indistinguishable — which silently dropped late payments.
    finalized = load(db_session, booking.id)
    assert finalized.finalized_at is not None
    assert finalized.calendar_event_id == event_id


def test_finalize_paid_booking_twice_sends_one_email(
    event_type, db_session, fake_calendar, fake_payments, recording_email, wired_deps
):
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    booking_service.mark_paid(db_session, booking, transaction_id="tx-1", paid_at=NOW)
    db_session.commit()

    tasks.finalize_paid_booking(booking.id)
    tasks.finalize_paid_booking(booking.id)

    assert len(recording_email.sent) == 1
    assert len(fake_calendar.confirmed) == 1


def test_finalize_does_nothing_unless_paid(
    event_type, db_session, fake_calendar, fake_payments, recording_email, wired_deps
):
    pending = make_hold(db_session, event_type, fake_calendar, fake_payments)
    expired = make_hold(
        db_session, event_type, fake_calendar, fake_payments, slot_start=SLOT2_UTC
    )
    expired.status = BookingStatus.EXPIRED
    db_session.commit()

    tasks.finalize_paid_booking(pending.id)
    tasks.finalize_paid_booking(expired.id)

    assert fake_calendar.confirmed == []
    assert recording_email.sent == []


# ---------------------------------------------------------------------------
# release_booking_hold
# ---------------------------------------------------------------------------


def test_release_booking_hold_is_idempotent(
    event_type, db_session, fake_calendar, fake_payments, wired_deps
):
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    event_id = booking.calendar_event_id

    tasks.release_booking_hold(booking.id)
    tasks.release_booking_hold(booking.id)

    assert fake_calendar.released == [event_id]
    assert load(db_session, booking.id).calendar_event_id is None


# ---------------------------------------------------------------------------
# Celery is lazy: importing app.tasks must not build or connect anything
# ---------------------------------------------------------------------------


def test_importing_app_tasks_without_a_broker_builds_no_celery(monkeypatch):
    """No broker configured ⇒ no Celery app, no Redis import, no connection."""
    monkeypatch.setattr(settings, "celery_broker_url", "")

    import celery

    def _forbidden_celery(*_args, **_kwargs):
        raise AssertionError("Celery() must not be constructed without a broker")

    monkeypatch.setattr(celery, "Celery", _forbidden_celery)

    import app.tasks as tasks_module

    importlib.reload(tasks_module)

    assert tasks_module.celery_app is None


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


def test_sweeper_sweep_once_releases_expired_holds(
    event_type, db_session, fake_calendar, fake_payments, wired_deps
):
    booking = make_hold(db_session, event_type, fake_calendar, fake_payments)
    event_id = booking.calendar_event_id
    _expire(db_session, booking)

    assert sweeper.sweep_once() == 1
    assert fake_calendar.released == [event_id]


class _StopLoop(Exception):
    """Sentinel used to break out of the infinite sweeper loop in a test."""


def test_sweeper_keeps_going_after_an_exception(monkeypatch):
    calls: list[int] = []

    def flaky_sweep() -> int:
        calls.append(1)
        raise RuntimeError("transient failure")

    monkeypatch.setattr(sweeper, "sweep_once", flaky_sweep)

    def stop_after_two(_seconds: float) -> None:
        if len(calls) >= 2:
            raise _StopLoop

    monkeypatch.setattr(sweeper.time, "sleep", stop_after_two)

    with pytest.raises(_StopLoop):
        sweeper.run_forever(interval_seconds=0)

    assert len(calls) == 2  # it swept again after the first failure


# ---------------------------------------------------------------------------
# Email adapters
# ---------------------------------------------------------------------------


def test_console_email_sender_prints_to_stdout(capsys):
    ConsoleEmailSender().send(to="alice@example.com", subject="预约确认", body="您好")
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert "预约确认" in out
    assert "您好" in out


class _CapturingTransport:
    """A no-socket stand-in for ``smtplib.SMTP_SSL``."""

    def __init__(self) -> None:
        self.messages: list = []
        self.login_args: tuple[str, str] | None = None

    def __enter__(self) -> _CapturingTransport:
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message) -> None:
        self.messages.append(message)


def test_smtp_email_sender_builds_a_utf8_message_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "smtp_user", "mailer@example.com")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "email_from", "noreply@zhituoyuan.com")

    transport = _CapturingTransport()
    sender = SmtpEmailSender(transport_factory=lambda: transport)

    subject = "预约确认 · BK7Q2M4X"
    body = "您的预约已确认。\n时间：2026-09-21 14:00（Asia/Shanghai）\n金额：¥500.00"
    sender.send(to="alice@example.com", subject=subject, body=body)

    assert transport.login_args == ("mailer@example.com", "secret")
    assert len(transport.messages) == 1
    raw = transport.messages[0].as_bytes()

    # The Chinese subject must be RFC 2047-encoded as UTF-8, not raw bytes.
    wire = raw.decode("ascii", errors="replace")
    assert "utf-8" in wire.lower()

    # Round trip through a real parse: the subject survives intact.
    parsed = message_from_bytes(raw)
    assert str(make_header(decode_header(parsed["Subject"]))) == subject

    payload = parsed.get_payload(decode=True)
    assert payload is not None
    assert payload.decode("utf-8").strip() == body.strip()
