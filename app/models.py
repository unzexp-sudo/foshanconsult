"""ORM models.  Authoritative — see docs/MODULE_CONTRACT.md §6.

Two invariants that everything else depends on:

* every datetime is timezone-aware UTC
* every amount is an integer number of 分 (fen), currency CNY
"""

from __future__ import annotations

import enum
import secrets
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db import utcnow

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    """Short human-readable booking code, e.g. BK7Q2M4X."""
    body = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
    return f"BK{body}"


def new_ticket_reference() -> str:
    """Short human-readable conference ticket code, e.g. TK7Q2M4X.

    Deliberately a different prefix from ``new_reference``'s ``BK…``: a reference
    is then self-describing, so the WeChat notify path can route to the right
    table without probing both, and a support question can be answered at a
    glance.
    """
    body = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
    return f"TK{body}"


class BookingStatus(enum.StrEnum):
    """Booking lifecycle state.

    ``StrEnum`` rather than ``(str, Enum)``: the members *are* the strings the DB
    stores, so ``f"{status}"`` and ``str(status)`` both yield ``"paid"`` rather
    than ``"BookingStatus.PAID"``.  The partial index predicate in
    ``__table_args__`` compares against those literal values.
    """
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# native_enum=False keeps this portable between SQLite and Postgres, and
# values_callable makes the DB store the *value* ("pending_payment") rather than the
# member name ("PENDING_PAYMENT") — the partial index predicate in §6 depends on it.
BookingStatusColumn = SAEnum(
    BookingStatus,
    name="booking_status",
    native_enum=False,
    length=32,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Base(DeclarativeBase):
    pass


class EventType(Base):
    """A bookable offering, e.g. "1-1 consultation, 30 minutes, ¥500"."""

    __tablename__ = "event_types"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price_fen: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, default=0)
    min_notice_minutes: Mapped[int] = mapped_column(Integer, default=240)
    max_days_ahead: Mapped[int] = mapped_column(Integer, default=60)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    availability_rules: Mapped[list[AvailabilityRule]] = relationship(
        back_populates="event_type",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AvailabilityRule(Base):
    """A recurring weekly window in which the event type may be booked."""

    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type_id: Mapped[str] = mapped_column(
        ForeignKey("event_types.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday ... 6 = Sunday
    start_local: Mapped[time] = mapped_column(Time)
    end_local: Mapped[time] = mapped_column(Time)

    event_type: Mapped[EventType] = relationship(back_populates="availability_rules")


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    reference: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    event_type_id: Mapped[str] = mapped_column(
        ForeignKey("event_types.id", ondelete="RESTRICT"), index=True
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[BookingStatus] = mapped_column(
        BookingStatusColumn, default=BookingStatus.PENDING_PAYMENT, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Snapshot taken at creation.  Never re-read EventType.price_fen at payment time —
    # the price the customer was shown is the price that must be charged.
    amount_fen: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")

    customer_name: Mapped[str] = mapped_column(String(200))
    customer_email: Mapped[str] = mapped_column(String(320))
    customer_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # Additive to contract §6 (integrator, 2026-09-19): the WeChat Native code_url is
    # persisted so /book/{reference} can re-render the QR without calling WeChat again.
    # It is a payment token — never expose it on a public, unauthenticated endpoint.
    code_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Additive to contract §6 (integrator, 2026-09-19): the "calendar confirmed +
    # confirmation email sent" marker.  Without it, `calendar_event_id IS NULL`
    # had to mean BOTH "already finalised" and "never held", so a booking whose
    # hold was released before the payment landed got no calendar event and no
    # email at all — a silent failure.  Keep the two ideas apart.
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # The double-booking guard.  Only live bookings occupy a slot; expired and
        # cancelled rows fall out of the predicate and free it again.
        Index(
            "uq_active_slot",
            "event_type_id",
            "slot_start",
            unique=True,
            sqlite_where=text("status IN ('pending_payment','paid')"),
            postgresql_where=text("status IN ('pending_payment','paid')"),
        ),
    )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Lazy expiry — the authoritative check (contract §7)."""
        if self.status is not BookingStatus.PENDING_PAYMENT:
            return False
        now = now or datetime.now(UTC)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:  # defensive: SQLite can hand back naive values
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= now

    @property
    def is_live(self) -> bool:
        return self.status in (BookingStatus.PENDING_PAYMENT, BookingStatus.PAID)


class PaymentEvent(Base):
    """Append-only record of every WeChat Pay callback.

    `transaction_id` is unique: WeChat resends a notification up to 15 times and this
    is the replay guard.
    """

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    out_trade_no: Mapped[str] = mapped_column(String(64), index=True)
    transaction_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="notify")
    raw_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(64), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Ticketed conferences — additive to contract §6 (2026-09-29)
# ---------------------------------------------------------------------------
#
# Deliberately separate tables rather than a `capacity` column on EventType.
# A 1-1 offering is a recurring weekly window with a slot grid and a calendar
# busy check; a conference is one fixed datetime with N identical seats.  They
# share almost no behaviour — and the difference is structural, not stylistic:
# `Booking.uq_active_slot` is UNIQUE(event_type_id, slot_start) over live rows,
# which permits exactly ONE live booking per start, so a 100-seat event cannot
# be represented in that table at all.  Seats get their own capacity guard.


class ConferenceEvent(Base):
    """A scheduled ticketed conference call — one fixed time, ``capacity`` seats."""

    __tablename__ = "conference_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    # One fixed instant, aware UTC like everything else.  No grid, no recurring
    # rule, no calendar lookup: the owner picks the date and time.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")

    # Integer 分, as everywhere.  A seat snapshots this at creation and never
    # re-reads it — the price shown is the price charged.
    price_fen: Mapped[int] = mapped_column(Integer, default=5000)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    capacity: Mapped[int] = mapped_column(Integer, default=100)

    # Where attendees go.  The owner pastes this from the meeting platform (VooV
    # Meeting, Zoom, WeCom…).  `join_note` carries what those platforms hand out
    # alongside the URL — a meeting number, a passcode — which is not part of it.
    join_url: Mapped[str] = mapped_column(Text, default="")
    join_note: Mapped[str] = mapped_column(Text, default="")
    # Reserved for a future auto-create integration.  Present from the start
    # because `init_db()` uses `create_all()`, which never ALTERs an existing
    # table — a column added later needs hand-written SQL against the live
    # Postgres.  Nullable and unused until something fills it.
    external_meeting_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    seats: Mapped[list[ConferenceSeat]] = relationship(
        back_populates="conference_event", cascade="all, delete-orphan"
    )

    @property
    def starts_at_utc(self) -> datetime:
        """SQLite can hand back naive values; the contract says everything is UTC."""
        return (
            self.starts_at
            if self.starts_at.tzinfo
            else self.starts_at.replace(tzinfo=UTC)
        )

    @property
    def ends_at(self) -> datetime:
        return self.starts_at_utc + timedelta(minutes=self.duration_minutes)

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None

    @property
    def join_ready(self) -> bool:
        """Is there a meeting link yet?

        Separate from :meth:`is_on_sale` so the public list can say *why* an event
        is not bookable.  "即将开放报名" and "已售罄" are different messages and a
        visitor deciding whether to come back needs the right one.
        """
        return bool(self.join_url.strip())

    def is_on_sale(self, now: datetime | None = None) -> bool:
        """Seats are sold until the conference starts.  Cancelled events sell none.

        A conference with no ``join_url`` is **also** not on sale, and that is
        deliberate: the meeting link is the entire product.  Selling a seat to an
        event whose link does not exist yet means a customer pays and receives a
        ticket that cannot tell them where to go — a refund and an apology, not a
        bug anyone can fix afterwards.  So the owner pastes the link first and the
        event goes on sale the moment they do; scheduling it early is still
        supported, it just is not sellable yet.
        """
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (
            self.active
            and not self.is_cancelled
            and self.join_ready
            and self.starts_at_utc > moment
        )


class ConferenceSeat(Base):
    """One seat at one :class:`ConferenceEvent` — the ticketing unit.

    The capacity guard is ``uq_live_seat``: a *partial* unique index on
    ``(conference_event_id, seat_no)`` over live rows only.  Allocating the lowest
    free seat number and retrying on ``IntegrityError`` is what makes 100 seats
    safe under concurrent payments — the index refuses the 101st insert, and an
    expired or cancelled seat drops out of the predicate and frees its number
    again.  The same shape as ``Booking.uq_active_slot``, generalised from one
    occupant per slot to ``capacity`` occupants.

    Because a live seat's number is in ``1..capacity`` and unique, "no more than
    ``capacity`` live seats" is a database invariant rather than a check some
    code path might forget.
    """

    __tablename__ = "conference_seats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    reference: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    conference_event_id: Mapped[str] = mapped_column(
        ForeignKey("conference_events.id", ondelete="RESTRICT"), index=True
    )
    seat_no: Mapped[int] = mapped_column(Integer)  # 1..ConferenceEvent.capacity

    status: Mapped[BookingStatus] = mapped_column(
        BookingStatusColumn, default=BookingStatus.PENDING_PAYMENT, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    amount_fen: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")

    customer_name: Mapped[str] = mapped_column(String(200))
    customer_email: Mapped[str] = mapped_column(String(320))
    customer_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    out_trade_no: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # A payment token, exactly as `Booking.code_url` is: rendered into an inline
    # SVG server-side and never returned to client JavaScript.
    code_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "The ticket email went out" marker — this table's counterpart to
    # `Booking.finalized_at`, and the reason a replayed notify cannot email twice.
    ticket_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    conference_event: Mapped[ConferenceEvent] = relationship(back_populates="seats")

    __table_args__ = (
        # The seat-count guard.  Only live seats occupy a number; expired and
        # cancelled rows fall out of the predicate and free it again.
        Index(
            "uq_live_seat",
            "conference_event_id",
            "seat_no",
            unique=True,
            sqlite_where=text("status IN ('pending_payment','paid')"),
            postgresql_where=text("status IN ('pending_payment','paid')"),
        ),
    )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Lazy expiry — the authoritative check, exactly as for a booking."""
        if self.status is not BookingStatus.PENDING_PAYMENT:
            return False
        now = now or datetime.now(UTC)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:  # defensive: SQLite can hand back naive values
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= now

    @property
    def is_live(self) -> bool:
        return self.status in (BookingStatus.PENDING_PAYMENT, BookingStatus.PAID)
