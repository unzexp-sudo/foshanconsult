"""Ticketed conferences — capacity, seats, and the money.

A conference is one fixed datetime with ``capacity`` identical seats, which makes
it a different problem from the 1-1 booking flow in three ways worth stating up
front, because each one is a bug if it is missed:

1. **Capacity is a database invariant, not a check.** ``ConferenceSeat.uq_live_seat``
   is a partial unique index on ``(conference_event_id, seat_no)`` over live rows.
   Allocating the lowest free seat number and retrying on ``IntegrityError`` means
   two simultaneous payments can never produce the 101st seat — the index refuses
   the insert, not a ``SELECT COUNT(*)`` that both requests already read.

2. **The index predicate is on ``status``, not on ``expires_at``.** A hold that has
   run out is *logically* dead (lazy expiry, contract §7) but is still physically
   ``pending_payment``, so it still occupies its seat number in the index.  The
   transactional pre-insert sweep is therefore what actually frees a number, and
   correctness must not depend on the sweeper running — same rule as bookings.

3. **Display and enforcement must read the same number.** :func:`seat_counts` is
   the single place the "seats taken" figure comes from, and the reservation path
   uses the same primitive underneath it.  A page that says 3 seats left while the
   booking path refuses the 98th seat is the defect class this project has already
   been bitten by twice.

The join link is stored, never generated: the owner creates the meeting in VooV
Meeting (or anything else) and pastes the URL.  It is a secret — it goes to a
buyer's inbox only after their payment verifies, and it must never appear on a
page or endpoint reachable before payment.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import utcnow
from app.models import (
    Booking,
    BookingStatus,
    ConferenceEvent,
    ConferenceSeat,
    new_ticket_reference,
)
from app.ports.payments import ChargeRequest, PaymentGateway

logger = logging.getLogger("booking")

__all__ = [
    "CapacityBelowSeats",
    "ConferenceError",
    "ConferenceNotFound",
    "SeatCounts",
    "SeatNotBookable",
    "SeatNotCancellable",
    "SeatNotFound",
    "SeatsClosed",
    "SoldOut",
    "cancel_event",
    "cancel_seat",
    "clashing_bookings",
    "create_event",
    "expire_stale_seats",
    "get_event",
    "get_seat",
    "honour_late_seat_payment",
    "list_all",
    "list_upcoming",
    "mark_seat_paid",
    "reserve_seat",
    "seat_counts",
    "seats_for",
    "update_event",
]

#: How many times to re-allocate when a concurrent request wins a seat number.
#: Each retry re-reads the taken set, so this only has to absorb the race window,
#: not a sustained contention storm.  Exhausting it means the event is effectively
#: sold out anyway, and the caller sees the same `SoldOut` a full event raises.
_MAX_ALLOCATION_ATTEMPTS = 8

#: The fields :func:`update_event` will write.  An allowlist rather than
#: ``setattr`` on whatever the caller sent: `id`, `created_at`, `cancelled_at` and
#: the seat rows are not the caller's to set, and a typo'd key silently doing
#: nothing is worse than an error.
_EDITABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "starts_at",
        "duration_minutes",
        "timezone",
        "price_fen",
        "capacity",
        "join_url",
        "join_note",
        "active",
    }
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ConferenceError(Exception):
    """Base class for every conference-domain failure."""


class ConferenceNotFound(ConferenceError):
    """No such conference event."""


class SeatsClosed(ConferenceError):
    """The event is cancelled, inactive, or already started — no seats are on sale."""


class SoldOut(ConferenceError):
    """Every seat is taken, counting unexpired holds."""


class SeatNotFound(ConferenceError):
    """No seat with that reference."""


class SeatNotBookable(ConferenceError):
    """The seat cannot be sold right now (bad state for this transition)."""


class SeatNotCancellable(ConferenceError):
    """Only a pending_payment seat may be cancelled."""


class CapacityBelowSeats(ConferenceError):
    """Lowering ``capacity`` would leave live seats outside the event's own size."""


@dataclass(frozen=True)
class SeatCounts:
    """One event's seat arithmetic, from one place."""

    capacity: int
    taken: int
    paid: int

    @property
    def available(self) -> int:
        return max(0, self.capacity - self.taken)

    @property
    def sold_out(self) -> bool:
        return self.available == 0


# ---------------------------------------------------------------------------
# Counting — the shared primitive
# ---------------------------------------------------------------------------


def _live_predicate(moment: datetime):
    """Paid, or a pending hold that has not expired (lazy expiry, contract §7.1)."""
    return (ConferenceSeat.status == BookingStatus.PAID) | (
        (ConferenceSeat.status == BookingStatus.PENDING_PAYMENT)
        & (ConferenceSeat.expires_at > moment)
    )


def _taken_seat_numbers(db: Session, event: ConferenceEvent, *, moment: datetime) -> set[int]:
    """Seat numbers currently occupied — the primitive both display and allocation use."""
    return set(
        db.scalars(
            select(ConferenceSeat.seat_no).where(
                ConferenceSeat.conference_event_id == event.id,
                _live_predicate(moment),
            )
        ).all()
    )


def seat_counts(db: Session, event: ConferenceEvent, *, now: datetime | None = None) -> SeatCounts:
    """Seats taken / available for one event.

    ``taken`` counts unexpired holds as well as paid seats, because a hold is
    genuinely not available to anyone else.  ``paid`` is the confirmed-attendee
    figure the owner cares about, reported separately rather than folded in, so a
    page never has to guess which question it is answering.
    """
    moment = _as_utc(now or utcnow())
    taken = _taken_seat_numbers(db, event, moment=moment)
    confirmed = db.scalar(
        select(func.count())
        .select_from(ConferenceSeat)
        .where(
            ConferenceSeat.conference_event_id == event.id,
            ConferenceSeat.status == BookingStatus.PAID,
        )
    )
    return SeatCounts(capacity=event.capacity, taken=len(taken), paid=int(confirmed or 0))


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def expire_stale_seats(
    db: Session,
    *,
    conference_event_id: str | None = None,
    seat_no: int | None = None,
    now: datetime | None = None,
) -> int:
    """Transactional pre-insert sweep.  Returns rows flipped.

    Flipping the status is the whole job, and it is what frees a seat *number*:
    the partial index keys on ``status``, so a hold that has merely run out still
    blocks its own number until this runs.  Lazy expiry keeps the arithmetic
    correct without it; this keeps the seat reusable.
    """
    moment = _as_utc(now or utcnow())
    stmt = (
        update(ConferenceSeat)
        .where(
            ConferenceSeat.status == BookingStatus.PENDING_PAYMENT,
            ConferenceSeat.expires_at <= moment,
        )
        .values(status=BookingStatus.EXPIRED)
        .execution_options(synchronize_session=False)
    )
    if conference_event_id is not None:
        stmt = stmt.where(ConferenceSeat.conference_event_id == conference_event_id)
    if seat_no is not None:
        stmt = stmt.where(ConferenceSeat.seat_no == seat_no)
    result = db.execute(stmt)
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get_event(db: Session, conference_id: str) -> ConferenceEvent | None:
    return db.get(ConferenceEvent, conference_id)


def get_seat(db: Session, reference: str) -> ConferenceSeat | None:
    return db.scalar(select(ConferenceSeat).where(ConferenceSeat.reference == reference))


def list_all(db: Session) -> list[ConferenceEvent]:
    """Every event, newest schedule first — the admin view.

    Unlike :func:`list_upcoming` this keeps cancelled and past events, because an
    owner who cancelled something needs to still see it, and the paid seats on it.
    """
    return list(
        db.scalars(select(ConferenceEvent).order_by(ConferenceEvent.starts_at.desc()))
    )


def seats_for(
    db: Session, event: ConferenceEvent, *, paid_only: bool = False
) -> list[ConferenceSeat]:
    """Every seat on an event, by seat number.  The attendee list."""
    stmt = select(ConferenceSeat).where(
        ConferenceSeat.conference_event_id == event.id
    )
    if paid_only:
        stmt = stmt.where(ConferenceSeat.status == BookingStatus.PAID)
    return list(db.scalars(stmt.order_by(ConferenceSeat.seat_no)))


def list_upcoming(
    db: Session, *, now: datetime | None = None, include_past: bool = False
) -> list[ConferenceEvent]:
    """Sellable events first, then any still-listed past ones when asked."""
    moment = _as_utc(now or utcnow())
    stmt = select(ConferenceEvent).where(ConferenceEvent.active.is_(True))
    if not include_past:
        stmt = stmt.where(
            ConferenceEvent.cancelled_at.is_(None),
            ConferenceEvent.starts_at > moment,
        )
    return list(db.scalars(stmt.order_by(ConferenceEvent.starts_at)))


def clashing_bookings(
    db: Session, event: ConferenceEvent, *, now: datetime | None = None
) -> list[str]:
    """Live 1-1 bookings that overlap this conference.

    The owner is one person, so a 1-1 consultation cannot happen during a
    conference they are hosting.  Reported to the admin caller rather than
    silently accepted; ``generate_slots`` enforces the other direction.
    """
    moment = _as_utc(now or utcnow())
    live = (Booking.status == BookingStatus.PAID) | (
        (Booking.status == BookingStatus.PENDING_PAYMENT) & (Booking.expires_at > moment)
    )
    return list(
        db.scalars(
            select(Booking.reference)
            .where(
                Booking.slot_start < event.ends_at,
                Booking.slot_end > event.starts_at_utc,
                live,
            )
            .order_by(Booking.slot_start)
        ).all()
    )


# ---------------------------------------------------------------------------
# Managing events (admin)
# ---------------------------------------------------------------------------


def create_event(
    db: Session,
    *,
    title: str,
    starts_at: datetime,
    description: str = "",
    duration_minutes: int = 60,
    timezone: str = settings.default_timezone,
    price_fen: int = 5000,
    capacity: int = 100,
    join_url: str = "",
    join_note: str = "",
    active: bool = True,
) -> ConferenceEvent:
    """Schedule a conference.  Sells nothing and touches no calendar.

    ``join_url`` is optional at this point on purpose — the owner usually books the
    VooV meeting after agreeing the date, and an event with no link yet is still a
    valid event to have on sale.  :func:`app.tasks.finalize_ticket` is what notices
    if a *paid* seat's event still has no link.
    """
    event = ConferenceEvent(
        id=f"conf_{uuid.uuid4().hex[:12]}",
        title=title,
        description=description,
        starts_at=_as_utc(starts_at),
        duration_minutes=duration_minutes,
        timezone=timezone,
        price_fen=price_fen,
        currency="CNY",
        capacity=capacity,
        join_url=join_url,
        join_note=join_note,
        active=active,
    )
    db.add(event)
    db.commit()
    return event


def update_event(db: Session, event: ConferenceEvent, **fields: object) -> ConferenceEvent:
    """Apply the supplied fields.  Anything omitted is left alone.

    The one real guard is ``capacity``.  Lowering it below the number of seats
    already held or paid would leave attendees outside the event's own definition
    of itself, and nothing downstream could repair that — the seats exist, the
    money is real, and the count would silently disagree with the roster.  Refuse,
    and report how many are taken so the owner can decide what to do instead.

    A ``None`` value means "not supplied" and is skipped, so clearing a text field
    is done with ``""`` rather than ``None``.
    """
    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"cannot edit {sorted(unknown)}")

    new_capacity = fields.get("capacity")
    if new_capacity is not None and int(new_capacity) != event.capacity:  # type: ignore[arg-type]
        counts = seat_counts(db, event)
        if int(new_capacity) < counts.taken:  # type: ignore[arg-type]
            raise CapacityBelowSeats(
                f"conference {event.id} has {counts.taken} seat(s) held or paid; "
                f"capacity cannot be lowered to {new_capacity}"
            )

    for name, value in fields.items():
        if value is None:
            continue
        setattr(event, name, _as_utc(value) if name == "starts_at" else value)  # type: ignore[arg-type]
    db.commit()
    return event


# ---------------------------------------------------------------------------
# Selling a seat
# ---------------------------------------------------------------------------


def reserve_seat(
    db: Session,
    *,
    conference_event_id: str,
    customer_name: str,
    customer_email: str,
    payments: PaymentGateway,
    customer_phone: str | None = None,
    now: datetime | None = None,
) -> ConferenceSeat:
    """Sweep → allocate the lowest free seat number → insert → WeChat Native order.

    The seat is *held*, not sold: it becomes real only when a verified payment
    lands (contract §7.1).  ``amount_fen`` is snapshotted from the event so a
    later price change cannot alter what this customer is charged.
    """
    moment = _as_utc(now or utcnow())
    event = db.get(ConferenceEvent, conference_event_id)
    if event is None:
        raise ConferenceNotFound(conference_event_id)
    if not event.is_on_sale(moment):
        raise SeatsClosed(
            f"conference {event.id} is not on sale "
            f"(active={event.active}, cancelled={event.is_cancelled}, "
            f"join_url={'set' if event.join_ready else 'MISSING'})"
        )

    # Free this event's dead holds in the same transaction, so the numbers they
    # were sitting on are allocatable below.  Scoped to the event: a global sweep
    # on the hot path would take locks on rows nobody is asking about.
    expire_stale_seats(db, conference_event_id=event.id, now=moment)

    seat: ConferenceSeat | None = None
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        taken = _taken_seat_numbers(db, event, moment=moment)
        seat_no = next(
            (candidate for candidate in range(1, event.capacity + 1) if candidate not in taken),
            None,
        )
        if seat_no is None:
            raise SoldOut(f"conference {event.id} is sold out ({event.capacity} seats)")

        candidate = ConferenceSeat(
            id=uuid.uuid4().hex,
            reference=new_ticket_reference(),
            conference_event_id=event.id,
            seat_no=seat_no,
            status=BookingStatus.PENDING_PAYMENT,
            expires_at=moment + timedelta(minutes=settings.hold_minutes),
            amount_fen=event.price_fen,  # snapshot — never re-read at pay time
            currency=event.currency,
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            out_trade_no=None,  # set to the reference once the order exists
        )
        try:
            # A savepoint, so losing the race for a seat number costs only this
            # insert — the sweep above and everything the caller did stay put.
            with db.begin_nested():
                db.add(candidate)
                db.flush()
        except IntegrityError:
            # Someone took that number between the SELECT and the INSERT.  The
            # savepoint is already rolled back; re-read and try the next one.
            logger.info(
                "reserve_seat: seat %s of %s was taken concurrently; re-allocating",
                seat_no,
                event.id,
            )
            continue
        seat = candidate
        break

    if seat is None:
        raise SoldOut(
            f"conference {event.id}: {_MAX_ALLOCATION_ATTEMPTS} allocation attempts "
            "all lost the race; treating as sold out"
        )

    seat.out_trade_no = seat.reference
    try:
        charge = payments.create_charge(
            ChargeRequest(
                out_trade_no=seat.reference,
                amount_fen=seat.amount_fen,
                description=f"{event.title} · 座位 {seat.seat_no}",
                expires_at=seat.expires_at,
            )
        )
        seat.code_url = charge.code_url
        db.commit()
    except Exception:
        # No calendar hold to unwind here, so a rollback is the whole cleanup —
        # the seat number goes back to the pool with it.
        db.rollback()
        raise

    return seat


def cancel_seat(
    db: Session, reference: str, *, now: datetime | None = None
) -> ConferenceSeat:
    """Cancel a pending seat and free its number immediately."""
    _ = _as_utc(now or utcnow())
    seat = get_seat(db, reference)
    if seat is None:
        raise SeatNotFound(reference)
    if seat.status is not BookingStatus.PENDING_PAYMENT:
        raise SeatNotCancellable(
            f"seat {reference} is {seat.status.value}, not pending_payment"
        )
    seat.status = BookingStatus.CANCELLED
    db.commit()
    return seat


# ---------------------------------------------------------------------------
# Settling payment
# ---------------------------------------------------------------------------


def mark_seat_paid(
    db: Session,
    seat: ConferenceSeat,
    *,
    transaction_id: str,
    paid_at: datetime | None = None,
) -> ConferenceSeat:
    """``pending_payment`` → ``paid``.  Idempotent; sends nothing.

    Deliberately does not commit: the notify handler owns the transaction so the
    seat transition and the ``PaymentEvent`` audit row land atomically.
    """
    if seat.status is not BookingStatus.PENDING_PAYMENT:
        return seat
    seat.status = BookingStatus.PAID
    seat.provider_transaction_id = transaction_id
    seat.paid_at = _as_utc(paid_at or utcnow())
    db.flush()
    return seat


def honour_late_seat_payment(
    db: Session,
    seat: ConferenceSeat,
    *,
    transaction_id: str,
    paid_at: datetime | None = None,
    now: datetime | None = None,
) -> ConferenceSeat:
    """Settle a verified payment that arrived after the hold stopped being live.

    The counterpart of :func:`app.services.booking.honour_late_payment`, with one
    extra way to fail that bookings do not have: the **event** can be cancelled.
    A verified payment is never dropped, so the preference order is:

    1. already ``paid`` → no-op.
    2. still ``pending_payment`` → the ordinary transition.
    3. ``expired`` or ``cancelled`` and the event is still on, and the seat number
       is still free → honour it.  They paid, v1 has no refunds, and the only
       non-harmful outcome is the seat.
    4. otherwise → :class:`~app.services.booking.PaymentConflict`.  Either the
       event was cancelled (every seat is off, so this needs a refund) or someone
       else holds the number now.
    """
    from app.services.booking import PaymentConflict

    moment = _as_utc(now or utcnow())

    if seat.status is BookingStatus.PAID:
        return seat
    if seat.status is BookingStatus.PENDING_PAYMENT:
        return mark_seat_paid(db, seat, transaction_id=transaction_id, paid_at=paid_at)

    previous_status = seat.status
    event = seat.conference_event
    if event is None or event.is_cancelled:
        raise PaymentConflict(
            f"seat {seat.reference} was {previous_status.value} and its conference is "
            f"cancelled; payment {transaction_id} needs a refund"
        )

    taken = _taken_seat_numbers(db, event, moment=moment)
    if seat.seat_no in taken:
        raise PaymentConflict(
            f"seat {seat.reference} was {previous_status.value} and seat "
            f"{seat.seat_no} is now held by someone else; payment {transaction_id} "
            "needs a refund"
        )

    seat.status = BookingStatus.PAID
    seat.provider_transaction_id = transaction_id
    seat.paid_at = _as_utc(paid_at or moment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise PaymentConflict(
            f"seat {seat.reference} could not be re-activated: its number was taken "
            f"while settling payment {transaction_id}"
        ) from exc
    logger.warning(
        "seat %s was %s but a verified payment arrived; honoured it because seat %s "
        "was still free",
        seat.reference,
        previous_status.value,
        seat.seat_no,
    )
    return seat


# ---------------------------------------------------------------------------
# Cancelling the whole event
# ---------------------------------------------------------------------------


def cancel_event(
    db: Session, event: ConferenceEvent, *, now: datetime | None = None
) -> list[str]:
    """Take a conference off sale and close every live seat.

    Returns the references of the seats that had **already been paid for** — the
    people owed either a refund or a replacement.  v1 issues no automatic refunds
    (contract §13), so this list is the work item: it is logged, returned to the
    admin caller, and every one of those rows keeps its ``paid_at`` and
    ``provider_transaction_id`` so the money can be traced in the WeChat console.
    """
    moment = _as_utc(now or utcnow())
    event.cancelled_at = moment
    event.active = False

    paid_references: list[str] = []
    seats = db.scalars(
        select(ConferenceSeat).where(
            ConferenceSeat.conference_event_id == event.id,
            ConferenceSeat.status.in_([BookingStatus.PENDING_PAYMENT, BookingStatus.PAID]),
        )
    ).all()
    for seat in seats:
        if seat.status is BookingStatus.PAID:
            paid_references.append(seat.reference)
        seat.status = BookingStatus.CANCELLED

    db.commit()
    if paid_references:
        logger.error(
            "conference %s cancelled with %d PAID seat(s) — refunds required: %s",
            event.id,
            len(paid_references),
            ", ".join(paid_references),
        )
    return paid_references
