"""Server-rendered pages — the payment surface (contract §11, owner M5).

``/`` is the 1-1 booking flow and ``/book/{reference}`` is the page a customer
lands on to pay for it.  ``/conferences``, ``/conference/{id}`` and
``/ticket/{reference}`` are the ticketed-conference equivalents (contract §14.4).
All are Chinese-language, mobile-first, and render with **no outbound request**:
the QR is drawn server-side and the stylesheet is served from ``app/static``.

Three invariants are load-bearing:

* ``Booking.code_url`` and ``ConferenceSeat.code_url`` are payment tokens.  They
  are consumed by :func:`qr_svg` and only the resulting SVG markup is rendered —
  never the token itself, and never in a data attribute or in JavaScript.
* ``ConferenceEvent.join_url`` is a *secret*, not a token: it is rendered only for
  a **paid** seat.  A ticket page is reachable by anyone holding the reference.
* Every timestamp is rendered in the *event's own* timezone.  The server's local
  zone is never used.

Money is integer 分 everywhere in the app; :func:`format_fen` is the single place
it becomes a ``¥`` string, at the display edge only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import BookingStatus, EventType
from app.services.booking import get_booking
from app.services.conference import (
    get_event,
    get_seat,
    list_upcoming,
    seat_counts,
)

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Display-only names for the zones we actually ship.  An unknown zone falls back
# to its raw IANA name, which is still a valid timezone label.
TZ_LABELS = {
    "Asia/Shanghai": "北京时间",
    "Asia/Hong_Kong": "香港时间",
    "Asia/Singapore": "新加坡时间",
    "Asia/Taipei": "台北时间",
    "UTC": "UTC",
}


def format_fen(amount_fen: int) -> str:
    """Format an integer 分 amount for display.

    ``50000 -> ¥500``, ``50123 -> ¥501.23``.  Nothing else in the app formats
    money; the DB and every service stay in 分.
    """
    yuan, cents = divmod(int(amount_fen), 100)
    return f"¥{yuan}.{cents:02d}" if cents else f"¥{yuan}"


def timezone_label(tz_name: str) -> str:
    return TZ_LABELS.get(tz_name, tz_name)


def aware_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; the contract says everything is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def local_time(value: datetime, tz_name: str) -> str:
    """``YYYY-MM-DD HH:MM`` in ``tz_name`` — never the server's local zone."""
    return aware_utc(value).astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")


def qr_svg(code_url: str) -> str:
    """Inline SVG markup for a WeChat Native ``code_url``.

    ``omitsize`` drops width/height so CSS can scale the code to the viewport
    while the ``viewBox`` keeps it crisp.  The token is consumed here and never
    returned — the SVG carries only the module geometry, not the URL.
    """
    code = segno.make(code_url, error="m")
    return code.svg_inline(omitsize=True, border=2, dark="#101010", light="#ffffff")


templates.env.filters["fen"] = format_fen
templates.env.filters["localtime"] = local_time
templates.env.globals["timezone_label"] = timezone_label


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def booking_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The booking flow.  The slot grid itself is fetched from ``/api/slots``."""
    event_types = list(
        db.scalars(
            select(EventType).where(EventType.active.is_(True)).order_by(EventType.id)
        )
    )
    return templates.TemplateResponse(
        request,
        "booking.html",
        {
            "event_types": event_types,
            "primary": event_types[0] if event_types else None,
            "hold_minutes": settings.hold_minutes,
            "default_timezone": settings.default_timezone,
        },
    )


@router.get("/book/{reference}", response_class=HTMLResponse, include_in_schema=False)
def booking_status_page(
    reference: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Payment + status page.  The QR is rendered here, server-side, or not at all."""
    booking = get_booking(db, reference.strip().upper())
    if booking is None:
        # A friendly Chinese page, not the API's raw JSON 404.
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"reference": reference},
            status_code=404,
        )

    event_type = db.get(EventType, booking.event_type_id)
    tz_name = event_type.timezone if event_type else settings.default_timezone
    now = datetime.now(UTC)

    # `Booking.is_expired(now)` is the authority on a stale hold (contract §7.1).
    if booking.status == BookingStatus.PAID:
        state = "paid"
    elif booking.status in (BookingStatus.EXPIRED, BookingStatus.CANCELLED) or booking.is_expired(
        now
    ):
        state = "dead"
    else:
        state = "pending"

    dead_reason = {
        BookingStatus.CANCELLED: "该订单已取消。",
        BookingStatus.EXPIRED: "支付超时，时段已释放。",
    }.get(booking.status, "支付超时，时段已释放。")

    # The QR exists only for a live, unexpired pending hold — and only as markup.
    svg: str | None = None
    if state == "pending" and booking.code_url:
        svg = qr_svg(booking.code_url)

    expires_at = aware_utc(booking.expires_at)
    remaining_seconds = max(0, int((expires_at - now).total_seconds()))

    return templates.TemplateResponse(
        request,
        "book_status.html",
        {
            "booking": booking,
            "event_title": event_type.title if event_type else booking.event_type_id,
            "tz_name": tz_name,
            "state": state,
            "dead_reason": dead_reason,
            "qr_svg": svg,
            "expires_at_iso": expires_at.isoformat(),
            "remaining_seconds": remaining_seconds,
        },
    )


# ---------------------------------------------------------------------------
# Ticketed conferences — contract §14.4
# ---------------------------------------------------------------------------
#
# Two things these pages must never do:
#
# * render `ConferenceEvent.join_url` unless the seat is **paid**.  The meeting
#   link is the thing a ticket buys; showing it on a page reachable by URL would
#   sell nothing.  `state == "paid"` is the only gate, and it is checked here, not
#   in the template.
# * show a seat count from anywhere other than `seat_counts`, so the figure a
#   visitor reads is the figure the reservation path enforces.


@router.get("/conferences", response_class=HTMLResponse, include_in_schema=False)
def conferences_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Upcoming conferences, each with seats taken and seats remaining."""
    rows = [
        {
            "event": event,
            "counts": seat_counts(db, event),
            "on_sale": event.is_on_sale(),
        }
        for event in list_upcoming(db)
    ]
    return templates.TemplateResponse(
        request,
        "conferences.html",
        {"rows": rows, "default_timezone": settings.default_timezone},
    )


@router.get(
    "/conference/{conference_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def conference_page(
    conference_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """One conference and its sign-up form.

    A cancelled or inactive event 404s rather than showing a dead page with a form
    that cannot succeed — the reservation endpoint would refuse it anyway, and a
    button that always fails is worse than no button.
    """
    event = get_event(db, conference_id)
    if event is None or not event.active or event.is_cancelled:
        return templates.TemplateResponse(
            request, "not_found.html", {"reference": conference_id}, status_code=404
        )

    return templates.TemplateResponse(
        request,
        "conference.html",
        {
            "event": event,
            "counts": seat_counts(db, event),
            "on_sale": event.is_on_sale(),
            "hold_minutes": settings.hold_minutes,
        },
    )


@router.get("/ticket/{reference}", response_class=HTMLResponse, include_in_schema=False)
def ticket_page(
    reference: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Pay for a held seat, then read the join link once it is paid."""
    seat = get_seat(db, reference.strip().upper())
    if seat is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"reference": reference}, status_code=404
        )

    event = get_event(db, seat.conference_event_id)
    if event is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"reference": reference}, status_code=404
        )

    now = datetime.now(UTC)

    # `Seat.is_expired(now)` is the authority on a stale hold, exactly as
    # `Booking.is_expired` is for a booking (contract §7.1).
    if seat.status == BookingStatus.PAID:
        state = "paid"
    elif seat.status in (BookingStatus.EXPIRED, BookingStatus.CANCELLED) or seat.is_expired(
        now
    ):
        state = "dead"
    else:
        state = "pending"

    if event.is_cancelled:
        # Every seat on a cancelled event is closed, so a paid one lands here too.
        # Say what actually happened instead of blaming the payment clock.
        dead_reason = "该场次已取消。如你已完成付款，我们会与你联系安排退款。"
    else:
        dead_reason = {
            BookingStatus.CANCELLED: "该座位已取消。",
            BookingStatus.EXPIRED: "支付超时，座位已释放。",
        }.get(seat.status, "支付超时，座位已释放。")

    # The QR exists only for a live, unexpired hold — and only as markup.
    svg: str | None = None
    if state == "pending" and seat.code_url:
        svg = qr_svg(seat.code_url)

    expires_at = aware_utc(seat.expires_at)
    remaining_seconds = max(0, int((expires_at - now).total_seconds()))

    return templates.TemplateResponse(
        request,
        "ticket.html",
        {
            "seat": seat,
            "event": event,
            "state": state,
            "dead_reason": dead_reason,
            "qr_svg": svg,
            "expires_at_iso": expires_at.isoformat(),
            "remaining_seconds": remaining_seconds,
            # Revealed only for a paid seat — the gate for the whole page.
            "join_url": event.join_url if state == "paid" else None,
            "join_note": event.join_note if state == "paid" else None,
        },
    )
