"""Request and response shapes for the public API (contract §11)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import BookingStatus


class EventTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str
    duration_minutes: int
    price_fen: int
    currency: str
    timezone: str


class SlotOut(BaseModel):
    start: datetime  # aware UTC
    end: datetime


class SlotsOut(BaseModel):
    event_type_id: str
    date: str  # local date, YYYY-MM-DD
    timezone: str
    slots: list[SlotOut]


class BookingCreateIn(BaseModel):
    event_type_id: str
    slot_start: datetime  # ISO-8601 with offset
    customer_name: str = Field(min_length=1, max_length=200)
    customer_email: EmailStr
    customer_phone: str | None = Field(default=None, max_length=64)
    customer_note: str | None = Field(default=None, max_length=2000)


class BookingCreatedOut(BaseModel):
    reference: str
    status: BookingStatus
    amount_fen: int
    currency: str
    expires_at: datetime
    slot_start: datetime
    code_url: str


class BookingStatusOut(BaseModel):
    reference: str
    status: BookingStatus
    amount_fen: int
    currency: str
    expires_at: datetime
    slot_start: datetime
    event_type_id: str


class AdminBookingOut(BookingStatusOut):
    customer_name: str
    customer_email: str
    paid_at: datetime | None = None
    created_at: datetime


class NotifyAck(BaseModel):
    code: str
    message: str


# ---------------------------------------------------------------------------
# Ticketed conferences
# ---------------------------------------------------------------------------


class ConferenceOut(BaseModel):
    """A conference as the *public* list shows it.

    Two fields are deliberately absent, and both matter:

    * ``join_url`` / ``join_note`` — the meeting link is a secret delivered only
      after a payment verifies.  Anything on this shape is reachable before one.
    * ``cancelled_at`` — a cancelled event simply disappears from the list, so a
      visitor has nothing to reason about.

    The counts are here because the owner asked for them: a buyer should see how
    full a call is before deciding.  They come from
    :func:`app.services.conference.seat_counts`, the same function the reservation
    path enforces against, so the page cannot advertise a seat the write path then
    refuses.
    """

    id: str
    title: str
    description: str
    starts_at: datetime  # aware UTC
    duration_minutes: int
    timezone: str
    price_fen: int
    currency: str
    capacity: int
    seats_taken: int
    seats_available: int
    seats_paid: int
    sold_out: bool
    #: ``False`` until the owner has pasted a meeting link.  Exposed so a list can
    #: say "not open yet" rather than "closed", which is what a visitor would
    #: otherwise conclude.
    join_ready: bool
    on_sale: bool


class ConferenceCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    starts_at: datetime  # ISO-8601 with offset
    duration_minutes: int = Field(default=60, ge=5, le=600)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    price_fen: int = Field(default=5000, ge=0)
    capacity: int = Field(default=100, ge=1, le=1000)
    join_url: str = Field(default="", max_length=2000)
    join_note: str = Field(default="", max_length=2000)
    active: bool = True


class ConferenceUpdateIn(BaseModel):
    """Every field optional — an omitted field is left unchanged."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    starts_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    timezone: str | None = Field(default=None, max_length=64)
    price_fen: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=1, le=1000)
    join_url: str | None = Field(default=None, max_length=2000)
    join_note: str | None = Field(default=None, max_length=2000)
    active: bool | None = None


class ConferenceAdminOut(ConferenceOut):
    """The owner's view: the join link, the on/off switch, the cancellation."""

    active: bool
    cancelled_at: datetime | None = None
    join_url: str
    join_note: str
    created_at: datetime


class ConferenceSavedOut(BaseModel):
    """A create/reschedule result, plus the clash warning.

    ``clashing_bookings`` lists live 1-1 bookings that overlap this conference.  The
    owner is one person, so this is a real mistake rather than a theoretical one —
    but it is reported, not refused: the conference is the commitment that cannot
    be moved once seats are sold, and a 1-1 is the one that can.
    """

    conference: ConferenceAdminOut
    clashing_bookings: list[str]


class ConferenceCancelOut(BaseModel):
    id: str
    cancelled_at: datetime
    paid_seats_needing_refund: list[str]


class SeatCreateIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    customer_email: EmailStr
    customer_phone: str | None = Field(default=None, max_length=64)


class SeatOut(BaseModel):
    reference: str
    seat_no: int
    status: BookingStatus
    amount_fen: int
    currency: str
    expires_at: datetime


class SeatCreatedOut(SeatOut):
    conference_id: str
    code_url: str


class SeatStatusOut(SeatOut):
    conference_id: str
    conference_title: str
    starts_at: datetime
    duration_minutes: int
    timezone: str
    paid_at: datetime | None = None
    #: Only ever populated once the seat is paid — see the page for why.
    join_url: str | None = None
    join_note: str | None = None


class AdminSeatOut(SeatOut):
    conference_id: str
    customer_name: str
    customer_email: str
    customer_phone: str | None = None
    paid_at: datetime | None = None
    ticket_sent_at: datetime | None = None
    created_at: datetime
