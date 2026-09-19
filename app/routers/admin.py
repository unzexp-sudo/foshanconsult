"""Read-only admin surface — contract §11.

``GET /api/admin/bookings`` requires ``X-Admin-Token`` to equal
``settings.secret_key``; the comparison is constant-time.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Booking
from app.schemas import AdminBookingOut

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
