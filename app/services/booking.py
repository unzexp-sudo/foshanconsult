"""Booking lifecycle service — contract §14.2.

The order of operations in :func:`create_booking` is load-bearing: validate the
grid, sweep stale holds **in the same transaction**, insert under the partial
unique index, and only then talk to the calendar and to WeChat.  The index — not
a SELECT-then-INSERT check — is what makes double booking impossible.

Services never import ``app.deps``; the router resolves the gateways and passes
them in.  That is what lets the tests swap in fakes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import utcnow
from app.models import Booking, BookingStatus, EventType, new_reference
from app.ports.calendar import CalendarGateway
from app.ports.payments import ChargeRequest, PaymentGateway
from app.services.availability import is_slot_on_grid, owned_intervals

logger = logging.getLogger("booking")

__all__ = [
    "BookingError",
    "BookingNotCancellable",
    "BookingNotFound",
    "EventTypeNotFound",
    "PaymentConflict",
    "SlotNotBookable",
    "SlotTaken",
    "booking_summary",
    "cancel_booking",
    "create_booking",
    "expire_stale_holds",
    "get_booking",
    "honour_late_payment",
    "mark_paid",
]


class BookingError(Exception):
    """Base class for every booking-domain failure."""


class EventTypeNotFound(BookingError):
    """No such event type, or it is inactive."""


class SlotNotBookable(BookingError):
    """The slot is off-grid, too soon, too far out, or otherwise not offered."""


class SlotTaken(BookingError):
    """Another live booking already holds the slot."""


class BookingNotFound(BookingError):
    """No booking with that reference."""


class BookingNotCancellable(BookingError):
    """Only a pending_payment booking may be cancelled."""


class PaymentConflict(BookingError):
    """The money arrived but the booking can no longer take it.

    Raised when a verified payment lands on a booking whose hold expired or was
    cancelled *and* whose slot has since been sold to someone else.  The caller
    must not acknowledge success — the slot cannot be taken back, so this needs a
    human and (in v1, which has no refunds) a manual refund.
    """


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def get_booking(db: Session, reference: str) -> Booking | None:
    return db.scalar(select(Booking).where(Booking.reference == reference))


def booking_summary(booking: Booking) -> str:
    """The human description shared by the calendar hold and the email."""
    start = _as_utc(booking.slot_start)
    end = _as_utc(booking.slot_end)
    tz = ZoneInfo(settings.default_timezone)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    yuan = f"{booking.amount_fen // 100}.{booking.amount_fen % 100:02d}"
    return "\n".join(
        [
            f"{booking.reference} · {booking.event_type_id}",
            f"{booking.customer_name} <{booking.customer_email}>",
            (
                f"{local_start:%Y-%m-%d %H:%M}–{local_end:%H:%M} "
                f"{settings.default_timezone}"
            ),
            f"{yuan} {booking.currency}",
        ]
    )


def expire_stale_holds(
    db: Session,
    *,
    event_type_id: str | None = None,
    slot_start: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Transactional pre-insert sweep (contract §7.2).  Returns rows flipped.

    Flipping the status is *all* it does — releasing the calendar hold and
    closing the WeChat order belong to the sweeper (§7.3).
    """
    moment = _as_utc(now or utcnow())
    stmt = (
        update(Booking)
        .where(
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.expires_at <= moment,
        )
        .values(status=BookingStatus.EXPIRED)
        .execution_options(synchronize_session=False)
    )
    if event_type_id is not None:
        stmt = stmt.where(Booking.event_type_id == event_type_id)
    if slot_start is not None:
        stmt = stmt.where(Booking.slot_start == _as_utc(slot_start))

    result = db.execute(stmt)
    return int(result.rowcount or 0)


def create_booking(
    db: Session,
    *,
    event_type_id: str,
    slot_start: datetime,
    customer_name: str,
    customer_email: str,
    calendar: CalendarGateway,
    payments: PaymentGateway,
    customer_phone: str | None = None,
    customer_note: str | None = None,
    now: datetime | None = None,
) -> Booking:
    """Validate → sweep → insert hold → calendar hold → WeChat charge → commit.

    ``amount_fen`` is snapshotted from ``EventType.price_fen``; the client never
    supplies it.  If the calendar or WeChat call fails the transaction is rolled
    back and any calendar hold is released — never a hold with no order.
    """
    moment = _as_utc(now or utcnow())
    start = _as_utc(slot_start)

    # 1. event type must exist and be active
    event_type = db.get(EventType, event_type_id)
    if event_type is None or not event_type.active:
        raise EventTypeNotFound(event_type_id)

    # 2. grid / notice / window
    if not is_slot_on_grid(db, event_type, start, now=moment):
        raise SlotNotBookable(f"slot {start.isoformat()} is not bookable")

    # 3. free the slot from any expired hold, in this same transaction
    expire_stale_holds(db, event_type_id=event_type.id, slot_start=start, now=moment)

    # 3b. the calendar is the source of truth for busy time.  `is_slot_on_grid`
    # cannot see it, and the DB index only guards against *our* bookings of THIS
    # event type, so without this an existing meeting is bookable by anyone who
    # POSTs a slot the grid never offered.  `owned_intervals` narrows the
    # calendar's authority only where the DB has already adjudicated — see its
    # docstring for which of our own intervals are ignored, and why a live hold of
    # a *different* event type must keep blocking.
    slot_end = start + timedelta(minutes=event_type.duration_minutes)
    owned = owned_intervals(db, start, slot_end, event_type_id=event_type.id, now=moment)
    for interval in calendar.freebusy(start, slot_end):
        busy_start, busy_end = _as_utc(interval.start), _as_utc(interval.end)
        if (busy_start, busy_end) in owned:
            continue
        if busy_start < slot_end and busy_end > start:
            raise SlotNotBookable(
                f"slot {start.isoformat()} overlaps existing calendar commitments"
            )

    # 4. insert under the partial unique index — the real double-booking guard
    reference = new_reference()
    booking = Booking(
        id=uuid.uuid4().hex,
        reference=reference,
        event_type_id=event_type.id,
        slot_start=start,
        slot_end=start + timedelta(minutes=event_type.duration_minutes),
        status=BookingStatus.PENDING_PAYMENT,
        expires_at=moment + timedelta(minutes=settings.hold_minutes),
        amount_fen=event_type.price_fen,  # snapshot — never re-read at pay time
        currency=event_type.currency,
        customer_name=customer_name,
        customer_email=str(customer_email),
        customer_phone=customer_phone,
        customer_note=customer_note,
        out_trade_no=reference,
    )
    db.add(booking)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise SlotTaken(f"slot {start.isoformat()} is already taken") from exc

    # 5 + 6. calendar hold, then WeChat Native order
    hold_event_id: str | None = None
    try:
        hold = calendar.create_hold(
            summary=f"HOLD · {customer_name} · {reference}",
            description=booking_summary(booking),
            start=booking.slot_start,
            end=booking.slot_end,
            reference=reference,
        )
        hold_event_id = hold.event_id
        booking.calendar_event_id = hold.event_id

        charge = payments.create_charge(
            ChargeRequest(
                out_trade_no=reference,
                amount_fen=booking.amount_fen,
                description=event_type.title,
                expires_at=booking.expires_at,
            )
        )
        booking.code_url = charge.code_url

        # 7. persist the hold and the order together
        db.commit()
    except Exception:
        db.rollback()
        if hold_event_id is not None:
            try:
                calendar.release(hold_event_id)
            except Exception:  # noqa: BLE001 - never mask the original failure
                logger.exception("failed to release calendar hold %s", hold_event_id)
        raise

    return booking


def cancel_booking(
    db: Session, reference: str, *, calendar: CalendarGateway, now: datetime | None = None
) -> Booking:
    """Cancel a ``pending_payment`` booking and release its calendar hold."""
    _ = _as_utc(now or utcnow())
    booking = get_booking(db, reference)
    if booking is None:
        raise BookingNotFound(reference)
    if booking.status is not BookingStatus.PENDING_PAYMENT:
        raise BookingNotCancellable(
            f"booking {reference} is {booking.status.value}, not pending_payment"
        )

    booking.status = BookingStatus.CANCELLED
    try:
        # Release before committing so a failure leaves the booking pending
        # rather than cancelled with a lingering calendar hold.
        if booking.calendar_event_id:
            calendar.release(booking.calendar_event_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return booking


def mark_paid(
    db: Session,
    booking: Booking,
    *,
    transaction_id: str,
    paid_at: datetime | None = None,
) -> Booking:
    """``pending_payment`` → ``paid``.  Idempotent; touches neither calendar nor email.

    Deliberately does not commit: the caller (the notify handler) owns the
    transaction so the booking transition and the PaymentEvent audit row land
    atomically.
    """
    if booking.status is BookingStatus.PAID:
        return booking
    if booking.status is not BookingStatus.PENDING_PAYMENT:
        return booking

    booking.status = BookingStatus.PAID
    booking.provider_transaction_id = transaction_id
    booking.paid_at = _as_utc(paid_at or utcnow())
    db.flush()
    return booking


def _slot_is_held_by_someone_else(
    db: Session, booking: Booking, *, now: datetime
) -> bool:
    """Is another *live* booking sitting on this booking's slot?"""
    live = (Booking.status == BookingStatus.PAID) | (
        (Booking.status == BookingStatus.PENDING_PAYMENT) & (Booking.expires_at > now)
    )
    other = db.scalar(
        select(Booking.id).where(
            Booking.id != booking.id,
            Booking.event_type_id == booking.event_type_id,
            Booking.slot_start == booking.slot_start,
            live,
        )
    )
    return other is not None


def honour_late_payment(
    db: Session,
    booking: Booking,
    *,
    transaction_id: str,
    paid_at: datetime | None = None,
    now: datetime | None = None,
) -> Booking:
    """Settle a verified payment that arrived after the hold stopped being live.

    A verified payment must **never** be silently dropped.  The customer's money
    is real even when their hold is not, so the preference order is:

    1. already ``paid`` → no-op.
    2. still ``pending_payment`` → the ordinary transition (delegates to
       :func:`mark_paid`).
    3. ``expired`` or ``cancelled`` but the slot is still free → honour it.  They
       paid; with no refunds in v1 the only non-harmful outcome is the call.
    4. ``expired`` or ``cancelled`` and the slot has since been sold → raise
       :class:`PaymentConflict`.  We will not steal a slot from the second
       customer, and we will not pretend the first one's payment succeeded.

    Flushes without committing — the caller owns the transaction so the booking
    transition and the ``PaymentEvent`` audit row land atomically.
    """
    moment = _as_utc(now or utcnow())

    if booking.status is BookingStatus.PAID:
        return booking

    if booking.status is BookingStatus.PENDING_PAYMENT:
        return mark_paid(db, booking, transaction_id=transaction_id, paid_at=paid_at)

    # Expired or cancelled: the hold is gone, but the money is not.
    previous_status = booking.status
    if _slot_is_held_by_someone_else(db, booking, now=moment):
        raise PaymentConflict(
            f"booking {booking.reference} is {previous_status.value} and its slot is "
            f"now held by another booking; payment {transaction_id} needs a refund"
        )

    booking.status = BookingStatus.PAID
    booking.provider_transaction_id = transaction_id
    booking.paid_at = _as_utc(paid_at or moment)
    try:
        db.flush()
    except IntegrityError as exc:
        # The partial unique index refused the resurrection: someone else won the
        # slot between our check and the flush.
        db.rollback()
        raise PaymentConflict(
            f"booking {booking.reference} could not be re-activated: its slot was "
            f"taken while settling payment {transaction_id}"
        ) from exc
    logger.warning(
        "booking %s was %s but a verified payment arrived; honoured it because the "
        "slot was still free",
        booking.reference,
        previous_status.value,
    )
    return booking
