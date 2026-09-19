"""ORM models.  Authoritative — see docs/MODULE_CONTRACT.md §6.

Two invariants that everything else depends on:

* every datetime is timezone-aware UTC
* every amount is an integer number of 分 (fen), currency CNY
"""

from __future__ import annotations

import enum
import secrets
from datetime import UTC, datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    JSON,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db import utcnow

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    """Short human-readable booking code, e.g. BK7Q2M4X."""
    body = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
    return f"BK{body}"


class BookingStatus(str, enum.Enum):
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

    availability_rules: Mapped[list["AvailabilityRule"]] = relationship(
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
