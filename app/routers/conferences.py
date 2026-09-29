"""Public ticketed-conference surface — contract §14.4.

Two rules shape this module, and both are about the join link.

**The meeting link is a secret.**  It goes to a buyer's inbox after a verified
payment, and it appears on no response this router serves before one — not the
list, not the detail, not a log line.  :class:`~app.schemas.SeatStatusOut` carries
``join_url``, and it is populated only for a paid seat.  That decision lives *here*
rather than in the template, because a template is the kind of file someone edits
without having read this paragraph.

**The counts come from one place.**  ``seat_counts`` is the same function
``reserve_seat`` enforces against, so a page can never advertise a seat the write
path then refuses — a defect class this project has been bitten by before.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_payment_gateway
from app.models import BookingStatus, ConferenceEvent, ConferenceSeat
from app.ports.payments import PaymentGateway
from app.rate_limit import rate_limit_seats
from app.schemas import ConferenceOut, SeatCreatedOut, SeatCreateIn, SeatStatusOut
from app.services import conference as conference_service
from app.services.conference import seat_counts

router = APIRouter(prefix="/api", tags=["conferences"])


def conference_out(db: Session, event: ConferenceEvent) -> ConferenceOut:
    """The public shape of an event.  Deliberately carries no join link."""
    counts = seat_counts(db, event)
    return ConferenceOut(
        id=event.id,
        title=event.title,
        description=event.description,
        starts_at=event.starts_at_utc,
        duration_minutes=event.duration_minutes,
        timezone=event.timezone,
        price_fen=event.price_fen,
        currency=event.currency,
        capacity=event.capacity,
        seats_taken=counts.taken,
        seats_available=counts.available,
        seats_paid=counts.paid,
        sold_out=counts.sold_out,
        join_ready=event.join_ready,
        on_sale=event.is_on_sale(),
    )


def seat_status_out(
    db: Session, seat: ConferenceSeat, event: ConferenceEvent
) -> SeatStatusOut:
    """The status shape, with the join link revealed only to a paid seat."""
    revealed = seat.status is BookingStatus.PAID
    return SeatStatusOut(
        reference=seat.reference,
        seat_no=seat.seat_no,
        status=seat.status,
        amount_fen=seat.amount_fen,
        currency=seat.currency,
        expires_at=seat.expires_at,
        conference_id=event.id,
        conference_title=event.title,
        starts_at=event.starts_at_utc,
        duration_minutes=event.duration_minutes,
        timezone=event.timezone,
        paid_at=seat.paid_at,
        join_url=event.join_url if revealed else None,
        join_note=event.join_note if revealed else None,
    )


def _seat_and_event(
    db: Session, reference: str
) -> tuple[ConferenceSeat, ConferenceEvent]:
    seat = conference_service.get_seat(db, reference.strip().upper())
    if seat is None:
        raise HTTPException(status_code=404, detail="unknown ticket")
    event = conference_service.get_event(db, seat.conference_event_id)
    if event is None:
        # Unreachable through the FK, but a seat whose event has vanished has no
        # time, no link and no title — a 404 is more honest than an empty page.
        raise HTTPException(status_code=404, detail="unknown conference")
    return seat, event


@router.get("/conferences", response_model=list[ConferenceOut])
def list_conferences(db: Session = Depends(get_db)) -> list[ConferenceOut]:
    """Upcoming events, soonest first, with seats taken and remaining."""
    return [conference_out(db, event) for event in conference_service.list_upcoming(db)]


@router.post(
    "/conferences/{conference_id}/seats",
    response_model=SeatCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def reserve_seat(
    conference_id: str,
    payload: SeatCreateIn,
    db: Session = Depends(get_db),
    payments: PaymentGateway = Depends(get_payment_gateway),
    _throttle: None = Depends(rate_limit_seats),
) -> SeatCreatedOut:
    """Hold one seat and open a WeChat Native order for it."""
    try:
        seat = conference_service.reserve_seat(
            db,
            conference_event_id=conference_id,
            customer_name=payload.customer_name,
            customer_email=str(payload.customer_email),
            customer_phone=payload.customer_phone,
            payments=payments,
        )
    except conference_service.ConferenceNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown conference") from exc
    except conference_service.SeatsClosed as exc:
        # Cancelled, inactive, or already started — 422 rather than 409, because
        # nothing was taken by anyone; there is simply nothing on sale.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except conference_service.SoldOut as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return SeatCreatedOut(
        reference=seat.reference,
        seat_no=seat.seat_no,
        status=seat.status,
        amount_fen=seat.amount_fen,
        currency=seat.currency,
        expires_at=seat.expires_at,
        conference_id=seat.conference_event_id,
        code_url=seat.code_url or "",
    )


@router.get("/seats/{reference}", response_model=SeatStatusOut)
def get_seat(reference: str, db: Session = Depends(get_db)) -> SeatStatusOut:
    seat, event = _seat_and_event(db, reference)
    return seat_status_out(db, seat, event)


@router.post("/seats/{reference}/cancel", response_model=SeatStatusOut)
def cancel_seat(reference: str, db: Session = Depends(get_db)) -> SeatStatusOut:
    """Give up a pending hold, freeing its seat number immediately."""
    try:
        seat = conference_service.cancel_seat(db, reference.strip().upper())
    except conference_service.SeatNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown ticket") from exc
    except conference_service.SeatNotCancellable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    event = conference_service.get_event(db, seat.conference_event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="unknown conference")
    return seat_status_out(db, seat, event)
