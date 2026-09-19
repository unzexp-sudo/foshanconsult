"""Public booking HTTP surface — contract §11.

Gateways are resolved with ``Depends(app.deps.get_*)`` and passed into the
service functions; the services themselves never import ``app.deps``.
"""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_calendar_gateway, get_payment_gateway
from app.models import Booking, EventType
from app.ports.calendar import CalendarGateway
from app.ports.payments import PaymentGateway
from app.schemas import (
    BookingCreatedOut,
    BookingCreateIn,
    BookingStatusOut,
    EventTypeOut,
    SlotOut,
    SlotsOut,
)
from app.services import booking as booking_service
from app.services.availability import generate_slots

router = APIRouter(prefix="/api", tags=["booking"])


def _status_out(booking: Booking) -> BookingStatusOut:
    return BookingStatusOut(
        reference=booking.reference,
        status=booking.status,
        amount_fen=booking.amount_fen,
        currency=booking.currency,
        expires_at=booking.expires_at,
        slot_start=booking.slot_start,
        event_type_id=booking.event_type_id,
    )


@router.get("/event-types", response_model=list[EventTypeOut])
def list_event_types(db: Session = Depends(get_db)) -> list[EventType]:
    return list(
        db.scalars(select(EventType).where(EventType.active.is_(True)).order_by(EventType.id))
    )


@router.get("/slots", response_model=SlotsOut)
def list_slots(
    event_type_id: str,
    date: date_type = Query(..., description="local date, YYYY-MM-DD"),
    db: Session = Depends(get_db),
    calendar: CalendarGateway = Depends(get_calendar_gateway),
) -> SlotsOut:
    event_type = db.get(EventType, event_type_id)
    if event_type is None or not event_type.active:
        raise HTTPException(status_code=404, detail="unknown event type")

    slots = generate_slots(db, event_type, date, calendar=calendar)
    return SlotsOut(
        event_type_id=event_type.id,
        date=date.isoformat(),
        timezone=event_type.timezone,
        slots=[SlotOut(start=slot.start, end=slot.end) for slot in slots],
    )


@router.post(
    "/bookings",
    response_model=BookingCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def create_booking(
    payload: BookingCreateIn,
    db: Session = Depends(get_db),
    calendar: CalendarGateway = Depends(get_calendar_gateway),
    payments: PaymentGateway = Depends(get_payment_gateway),
) -> BookingCreatedOut:
    if payload.slot_start.tzinfo is None:
        raise HTTPException(
            status_code=422, detail="slot_start must be ISO-8601 with an offset"
        )

    try:
        booking = booking_service.create_booking(
            db,
            event_type_id=payload.event_type_id,
            slot_start=payload.slot_start,
            customer_name=payload.customer_name,
            customer_email=str(payload.customer_email),
            customer_phone=payload.customer_phone,
            customer_note=payload.customer_note,
            calendar=calendar,
            payments=payments,
        )
    except booking_service.SlotTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except booking_service.SlotNotBookable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except booking_service.EventTypeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return BookingCreatedOut(
        reference=booking.reference,
        status=booking.status,
        amount_fen=booking.amount_fen,
        currency=booking.currency,
        expires_at=booking.expires_at,
        slot_start=booking.slot_start,
        code_url=booking.code_url or "",
    )


@router.get("/bookings/{reference}", response_model=BookingStatusOut)
def get_booking(reference: str, db: Session = Depends(get_db)) -> BookingStatusOut:
    booking = booking_service.get_booking(db, reference)
    if booking is None:
        raise HTTPException(status_code=404, detail="unknown booking")
    return _status_out(booking)


@router.post("/bookings/{reference}/cancel", response_model=BookingStatusOut)
def cancel_booking(
    reference: str,
    db: Session = Depends(get_db),
    calendar: CalendarGateway = Depends(get_calendar_gateway),
) -> BookingStatusOut:
    try:
        booking = booking_service.cancel_booking(db, reference, calendar=calendar)
    except booking_service.BookingNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown booking") from exc
    except booking_service.BookingNotCancellable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_out(booking)
