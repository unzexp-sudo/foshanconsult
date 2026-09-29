"""Admin surface — contract §11 and §14.4.

Every endpoint here requires ``X-Admin-Token`` to equal ``settings.secret_key``;
the comparison is constant-time.  The token is a shared secret rather than a user
account, so it identifies the owner and nothing finer — which is why the
conference endpoints report consequences (a clash, a refund list) instead of
assuming a human read the response.

``GET /api/admin/bookings`` is read-only.  The conference endpoints are not:
scheduling, editing and cancelling are the whole point of them, and they are the
only write surface the owner has.  A cancelled conference with paid seats is the
one operation that can leave somebody owed money, so it returns the references
rather than just a 200.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Booking, ConferenceEvent

# `conference_out` is the one place the public shape — and therefore the seat
# counts — is assembled.  Importing it rather than re-deriving the counts here is
# deliberate: two builders would be two chances for the admin figure and the
# public figure to disagree about the same event.
from app.routers.conferences import conference_out
from app.schemas import (
    AdminBookingOut,
    AdminSeatOut,
    ConferenceAdminOut,
    ConferenceCancelOut,
    ConferenceCreateIn,
    ConferenceSavedOut,
    ConferenceUpdateIn,
)
from app.services import conference as conference_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    expected = settings.secret_key.encode("utf-8")
    provided = (x_admin_token or "").encode("utf-8")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


@router.get("/bookings", response_model=list[AdminBookingOut])
def list_bookings(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> list[AdminBookingOut]:
    rows = db.scalars(
        select(Booking).order_by(Booking.created_at.desc(), Booking.reference.desc())
    ).all()
    return [
        AdminBookingOut(
            reference=booking.reference,
            status=booking.status,
            amount_fen=booking.amount_fen,
            currency=booking.currency,
            expires_at=booking.expires_at,
            slot_start=booking.slot_start,
            event_type_id=booking.event_type_id,
            customer_name=booking.customer_name,
            customer_email=booking.customer_email,
            paid_at=booking.paid_at,
            created_at=booking.created_at,
        )
        for booking in rows
    ]


# ---------------------------------------------------------------------------
# Conferences
# ---------------------------------------------------------------------------


def _admin_out(db: Session, event: ConferenceEvent) -> ConferenceAdminOut:
    """The owner's shape: the public one, plus the link and the switch."""
    return ConferenceAdminOut(
        **conference_out(db, event).model_dump(),
        active=event.active,
        cancelled_at=event.cancelled_at,
        join_url=event.join_url,
        join_note=event.join_note,
        created_at=event.created_at,
    )


def _require_event(db: Session, conference_id: str) -> ConferenceEvent:
    event = conference_service.get_event(db, conference_id)
    if event is None:
        raise HTTPException(status_code=404, detail="unknown conference")
    return event


def _require_offset(value, field: str = "starts_at"):
    if value is not None and value.tzinfo is None:
        raise HTTPException(
            status_code=422, detail=f"{field} must be ISO-8601 with an offset"
        )
    return value


@router.get("/conferences", response_model=list[ConferenceAdminOut])
def list_conferences(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> list[ConferenceAdminOut]:
    """Every event, including cancelled and past ones, newest schedule first."""
    return [_admin_out(db, event) for event in conference_service.list_all(db)]


@router.post(
    "/conferences",
    response_model=ConferenceSavedOut,
    status_code=status.HTTP_201_CREATED,
)
def create_conference(
    payload: ConferenceCreateIn,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> ConferenceSavedOut:
    """Schedule a conference.  ``join_url`` may be added later."""
    _require_offset(payload.starts_at)
    event = conference_service.create_event(db, **payload.model_dump())
    return ConferenceSavedOut(
        conference=_admin_out(db, event),
        clashing_bookings=conference_service.clashing_bookings(db, event),
    )


@router.patch("/conferences/{conference_id}", response_model=ConferenceSavedOut)
def update_conference(
    conference_id: str,
    payload: ConferenceUpdateIn,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> ConferenceSavedOut:
    """Reschedule, re-price, edit the link, or change capacity.

    ``exclude_unset`` so an omitted field is genuinely absent rather than an
    explicit ``null`` — the two mean different things to a PATCH, and treating
    them the same is how a caller accidentally blanks a field.
    """
    event = _require_event(db, conference_id)
    fields = payload.model_dump(exclude_unset=True)
    _require_offset(fields.get("starts_at"))
    try:
        event = conference_service.update_event(db, event, **fields)
    except conference_service.CapacityBelowSeats as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConferenceSavedOut(
        conference=_admin_out(db, event),
        clashing_bookings=conference_service.clashing_bookings(db, event),
    )


@router.post("/conferences/{conference_id}/cancel", response_model=ConferenceCancelOut)
def cancel_conference(
    conference_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> ConferenceCancelOut:
    """Take an event off sale and close every seat on it.

    The response lists the seats that had already been paid for.  v1 issues no
    automatic refunds (contract §13), so that list *is* the follow-up work: those
    people are owed a refund or a replacement, and this is the only place they are
    named.  Ignoring it silently is the failure mode this shape exists to prevent.
    """
    event = _require_event(db, conference_id)
    paid_references = conference_service.cancel_event(db, event)
    return ConferenceCancelOut(
        id=event.id,
        cancelled_at=event.cancelled_at,
        paid_seats_needing_refund=paid_references,
    )


@router.get("/conferences/{conference_id}/seats", response_model=list[AdminSeatOut])
def list_conference_seats(
    conference_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> list[AdminSeatOut]:
    """The attendee list, by seat number, live and dead rows included."""
    event = _require_event(db, conference_id)
    return [
        AdminSeatOut(
            reference=seat.reference,
            seat_no=seat.seat_no,
            status=seat.status,
            amount_fen=seat.amount_fen,
            currency=seat.currency,
            expires_at=seat.expires_at,
            conference_id=seat.conference_event_id,
            customer_name=seat.customer_name,
            customer_email=seat.customer_email,
            customer_phone=seat.customer_phone,
            paid_at=seat.paid_at,
            ticket_sent_at=seat.ticket_sent_at,
            created_at=seat.created_at,
        )
        for seat in conference_service.seats_for(db, event)
    ]
