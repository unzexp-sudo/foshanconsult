"""Background tasks — contract §10 and §14.3.

Three plain, synchronously-callable functions.  Dev runs them inline from FastAPI
``BackgroundTasks`` (contract §10); prod wraps the very same functions as Celery
tasks.  Importing this module must never require Redis and must never open a
connection, so the Celery app is built lazily and only when a broker is configured.

Marker conventions — all three are load-bearing:

* ``Booking.calendar_event_id`` is the *outstanding calendar work* marker.  A non-null
  value on an ``expired`` row means "this hold still needs releasing".  Once the
  release succeeds the column is cleared to ``None``.  That is why
  :func:`release_expired_holds` selects on ``calendar_event_id IS NOT NULL`` and why
  running it twice is a no-op.
* ``Booking.finalized_at`` (integrator addition, contract §15) marks "calendar
  confirmed **and** confirmation email sent".  It exists because the earlier design
  overloaded ``calendar_event_id IS NULL`` to mean both "already finalised" and
  "never held" — so a booking whose hold was released before a late payment landed
  got no calendar event and no email at all, silently.  Keeping the two ideas in one
  column is what produced that bug; do not re-merge them.
* ``ConferenceSeat.ticket_sent_at`` is this table's counterpart to
  ``finalized_at``, and it guards a *secret*: the ticket email carries the join
  link, so a replayed notify that emailed twice would hand out a second copy of it.
  A conference has no calendar marker at all — the event belongs to the owner, not
  to us — which is why this marker is the only one it needs.

Correctness never depends on any of this: §7's lazy expiry and the transactional
pre-insert sweep free slots on their own.  These tasks are hygiene only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import deps
from app.config import settings
from app.db import session_scope, utcnow
from app.models import (
    Booking,
    BookingStatus,
    ConferenceEvent,
    ConferenceSeat,
    EventType,
)
from app.services.booking import booking_summary

logger = logging.getLogger("booking")

__all__ = [
    "celery_app",
    "finalize_paid_booking",
    "finalize_ticket",
    "release_booking_hold",
    "release_expired_holds",
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# §7.3 — the hygiene sweeper body
# ---------------------------------------------------------------------------


def release_expired_holds() -> int:
    """Release calendar holds and close WeChat orders for ``expired`` rows.

    Returns the number of bookings processed.  Selecting on
    ``calendar_event_id IS NOT NULL`` is the whole idempotency story: once a row has
    been released the marker is cleared, so the next sweep does not see it again.

    ``calendar.release`` is idempotent by contract §8, so a crash between the release
    and the commit only costs a redundant, harmless delete on the next sweep.
    """
    calendar = deps.get_calendar_gateway()
    payments = deps.get_payment_gateway()

    processed = 0
    with session_scope() as db:
        rows = db.scalars(
            select(Booking).where(
                Booking.status == BookingStatus.EXPIRED,
                Booking.calendar_event_id.is_not(None),
            )
        ).all()

        for booking in rows:
            event_id = booking.calendar_event_id
            try:
                calendar.release(event_id)
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.exception(
                    "release_expired_holds: calendar.release(%r) failed; retrying next sweep",
                    event_id,
                )
                continue

            if booking.out_trade_no:
                _close_order_best_effort(payments, booking.out_trade_no)

            # Released-marker: the hold is gone, this row is done.
            booking.calendar_event_id = None
            processed += 1

    if processed:
        logger.info("release_expired_holds: released %d expired hold(s)", processed)
    return processed


def _close_order_best_effort(payments: Any, out_trade_no: str) -> None:
    """Close the WeChat order, tolerating "already closed" and any other failure.

    The real adapter already treats ``ORDER_CLOSED`` as success and does not raise
    (M2's contract test asserts exactly that), so any exception reaching here is a
    genuine failure.  It is still swallowed: WeChat closes unpaid Native orders by
    itself at ``time_expire``, so the worst case is a short-lived open order, while
    the calendar release — the part that actually frees the slot — has already
    succeeded and its marker must be cleared.
    """
    try:
        payments.close_order(out_trade_no)
    except Exception as exc:  # noqa: BLE001 - best effort by design
        logger.warning(
            "release_expired_holds: close_order(%r) failed; WeChat will auto-close at "
            "time_expire: %r",
            out_trade_no,
            exc,
        )


# ---------------------------------------------------------------------------
# §10 — finalize a paid booking
# ---------------------------------------------------------------------------


def finalize_paid_booking(booking_id: str) -> None:
    """Confirm the calendar hold and email the customer, once.

    Called from a FastAPI ``BackgroundTask`` after the notify response has already
    been sent, so it must never raise: any failure is logged with a stack trace and
    swallowed.  Idempotent by the ``calendar_event_id`` marker — a second call is a
    no-op and does not re-send the email.
    """
    try:
        _finalize_paid_booking(booking_id)
    except Exception:  # noqa: BLE001 - a background task must never break the response
        logger.exception("finalize_paid_booking failed for booking %s", booking_id)


def _finalize_paid_booking(booking_id: str) -> None:
    calendar = deps.get_calendar_gateway()
    email = deps.get_email_sender()

    # Any exception here propagates out of the ``with``, so ``session_scope`` rolls
    # back and the marker stays unset — the work is retried rather than lost.
    with session_scope() as db:
        booking = db.get(Booking, booking_id)
        if booking is None:
            logger.warning("finalize_paid_booking: booking %s not found", booking_id)
            return
        if booking.status is not BookingStatus.PAID:
            return
        if booking.finalized_at is not None:
            return  # already confirmed and emailed — never double-send

        event_type = db.get(EventType, booking.event_type_id)
        title = event_type.title if event_type is not None else booking.event_type_id
        summary = f"{title} · {booking.customer_name}"
        description = booking_summary(booking)

        if booking.calendar_event_id is not None:
            calendar.confirm(
                booking.calendar_event_id, summary=summary, description=description
            )
        else:
            # No hold to confirm: it was released before the payment landed, which
            # is exactly what happens when a verified payment arrives after the
            # hold expired.  Create the event now rather than leave the owner with
            # no invitation and the customer with a booking nobody can see.
            created = calendar.create_hold(
                summary=summary,
                description=description,
                start=_as_utc(booking.slot_start),
                end=_as_utc(booking.slot_end),
                reference=booking.reference,
            )
            calendar.confirm(created.event_id, summary=summary, description=description)
            booking.calendar_event_id = created.event_id

        subject, body = _confirmation_email(booking)
        email.send(to=booking.customer_email, subject=subject, body=body)

        booking.finalized_at = utcnow()


def _confirmation_email(booking: Booking) -> tuple[str, str]:
    """Chinese confirmation subject and body (amounts formatted only here)."""
    tz = ZoneInfo(settings.default_timezone)
    start = _as_utc(booking.slot_start).astimezone(tz)
    end = _as_utc(booking.slot_end).astimezone(tz)
    yuan = f"¥{booking.amount_fen // 100}.{booking.amount_fen % 100:02d}"
    link = f"{settings.public_base_url.rstrip('/')}/book/{booking.reference}"

    subject = f"预约确认 · {booking.reference}"
    body = "\n".join(
        [
            f"{booking.customer_name}，您好：",
            "",
            "您的 1-1 咨询预约已确认，期待与您交流。",
            "",
            f"预约编号：{booking.reference}",
            f"时间：{start:%Y-%m-%d %H:%M}–{end:%H:%M}（{settings.default_timezone}）",
            f"金额：{yuan}",
            f"详情：{link}",
            "",
            "如需改期，请直接回复本邮件。",
        ]
    )
    return subject, body


# ---------------------------------------------------------------------------
# §14.4 — finalize a paid conference seat
# ---------------------------------------------------------------------------


def finalize_ticket(seat_id: str) -> None:
    """Email the attendee their ticket and join link, once.

    Dispatched from a FastAPI ``BackgroundTask`` after the notify response has
    already been sent, so it must never raise — same contract as
    :func:`finalize_paid_booking`.  Idempotent by the ``ticket_sent_at`` marker.

    There is **no calendar step here**, unlike :func:`finalize_paid_booking`.  A
    conference is one event the owner created in their own meeting platform, not a
    hold we placed on their behalf, so there is nothing to confirm and nothing to
    release.  The email is the whole job.
    """
    try:
        _finalize_ticket(seat_id)
    except Exception:  # noqa: BLE001 - a background task must never break the response
        logger.exception("finalize_ticket failed for seat %s", seat_id)


def _finalize_ticket(seat_id: str) -> None:
    email = deps.get_email_sender()

    # Any exception here propagates out of the ``with``, so ``session_scope`` rolls
    # back and the marker stays unset — the ticket is retried rather than lost.
    with session_scope() as db:
        seat = db.get(ConferenceSeat, seat_id)
        if seat is None:
            logger.warning("finalize_ticket: seat %s not found", seat_id)
            return
        if seat.status is not BookingStatus.PAID:
            return
        if seat.ticket_sent_at is not None:
            return  # already emailed — never send the join link a second time

        event = db.get(ConferenceEvent, seat.conference_event_id)
        if event is None:
            # Unreachable through the FK, but it would mean a paid customer we
            # cannot tell where to go.  Leave the marker unset so a retry can
            # still send, and say so loudly.
            logger.error(
                "finalize_ticket: seat %s references missing conference %s; "
                "no ticket sent",
                seat.reference,
                seat.conference_event_id,
            )
            return

        if not event.join_url:
            # The owner scheduled the event without pasting a link.  The seat is
            # paid and the email is about to go out without the one thing it is
            # for — a work item for a human, not something to swallow.
            logger.error(
                "finalize_ticket: conference %s has no join_url; ticket %s is paid "
                "and cannot carry a meeting link — the owner must send it by hand",
                event.id,
                seat.reference,
            )

        subject, body = _ticket_email(seat, event)
        email.send(to=seat.customer_email, subject=subject, body=body)

        seat.ticket_sent_at = utcnow()


def _ticket_email(seat: ConferenceSeat, event: ConferenceEvent) -> tuple[str, str]:
    """Chinese ticket subject and body.

    The join link is the point of this email **and it is a secret**: it is
    delivered here, after a verified payment, and deliberately appears on no page
    or endpoint reachable before one.  Never move it into the confirmation page,
    the public API, or a log line.

    Times render in the conference's own timezone, never the server's — the same
    rule the booking email follows.  Note that this prints the raw IANA name
    (``Asia/Shanghai``) rather than the Chinese label the pages show
    (``北京时间``); that is what the existing booking email does, and changing it
    is a product decision, not a formatting one.
    """
    tz = ZoneInfo(event.timezone)
    start = _as_utc(event.starts_at).astimezone(tz)
    end = start + timedelta(minutes=event.duration_minutes)
    yuan = f"¥{seat.amount_fen // 100}.{seat.amount_fen % 100:02d}"
    link = f"{settings.public_base_url.rstrip('/')}/ticket/{seat.reference}"

    subject = f"报名确认 · {event.title} · {seat.reference}"
    lines = [
        f"{seat.customer_name}，您好：",
        "",
        f"您已成功报名「{event.title}」，座位已为您保留。",
        "",
        f"票号：{seat.reference}（第 {seat.seat_no} 号座位）",
        f"时间：{start:%Y-%m-%d %H:%M}–{end:%H:%M}（{event.timezone}）",
        f"金额：{yuan}",
        "",
        "会议链接：",
        event.join_url or "（主办方将尽快通过邮件另行发送）",
    ]
    if event.join_note:
        lines += ["", f"入会说明：{event.join_note}"]
    lines += [
        "",
        f"详情：{link}",
        "",
        "建议提前几分钟进入会议。如需协助，请直接回复本邮件。",
    ]
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# §10 — release a single hold (cancel path)
# ---------------------------------------------------------------------------


def release_booking_hold(booking_id: str) -> None:
    """Release one booking's calendar hold.  Idempotent.

    Only the calendar hold is touched: the WeChat order of a cancelled booking is
    left to expire on its own at ``time_expire`` (contract §13 keeps refunds out of
    v1).  A second call sees ``calendar_event_id`` cleared and does nothing.
    """
    try:
        _release_booking_hold(booking_id)
    except Exception:  # noqa: BLE001 - dispatched as a background task
        logger.exception("release_booking_hold failed for booking %s", booking_id)


def _release_booking_hold(booking_id: str) -> None:
    calendar = deps.get_calendar_gateway()
    with session_scope() as db:
        booking = db.get(Booking, booking_id)
        if booking is None or booking.calendar_event_id is None:
            return
        calendar.release(booking.calendar_event_id)
        booking.calendar_event_id = None


# ---------------------------------------------------------------------------
# Celery — prod only, and lazy
# ---------------------------------------------------------------------------
#
# Contract §10 freezes the *task names* ``app.tasks.<function>``.  In dev there is no
# broker, so nothing here runs: the plain functions above are imported and called
# directly (and by ``app.services.sweeper``).  Only when a broker is configured do we
# build the app, and even then constructing a ``Celery`` object opens no socket —
# connections happen on the first task dispatch.

celery_app: Any = None


def _build_celery_app() -> Any:
    from celery import Celery

    app = Celery(
        "booking",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend or None,
    )
    for func in (
        release_expired_holds,
        finalize_paid_booking,
        finalize_ticket,
        release_booking_hold,
    ):
        # Registers under the frozen name but leaves the module attribute as the plain
        # function, which is what dev and the tests call.
        app.task(name=f"app.tasks.{func.__name__}")(func)
    return app


if settings.celery_broker_url:
    celery_app = _build_celery_app()
