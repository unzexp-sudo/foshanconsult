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
