"""WeChat Pay callback endpoint — contract §11 / BUILD_PLAN §6.

There is no auth header: authenticity *is* the signature, so the raw body is read
once and handed to the gateway unmodified.  The handler owns the transaction
because :func:`app.services.booking.mark_paid` deliberately only flushes — the
booking transition and the ``PaymentEvent`` audit row must land atomically.

WeChat retries a callback up to 15 times, so every failure path answers 2xx
(except a forged signature, which is not a payment event at all) and does the
minimum possible work.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_payment_gateway
from app.models import Booking, BookingStatus, PaymentEvent
from app.ports.payments import PaymentGateway, PaymentSignatureError
from app.services.booking import PaymentConflict, honour_late_payment

logger = logging.getLogger("booking")

router = APIRouter(prefix="/api/payments", tags=["payments"])

NOTIFY_PATH = "/api/payments/wechat/notify"


def _ack(code: str, message: str, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message})


def _coerce_datetime(value: object) -> datetime | None:
    """Tolerate a provider (or fake) that hands back an ISO string instead of a
    datetime, so a well-formed payment can never be rejected over formatting."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _record(
    db: Session,
    *,
    out_trade_no: str,
    transaction_id: str | None,
    headers: dict[str, str],
    raw_text: str,
    outcome: str,
) -> bool:
    """Append an audit row.  Returns False when it is a duplicate transaction."""
    db.add(
        PaymentEvent(
            out_trade_no=out_trade_no,
            transaction_id=transaction_id,
            kind="notify",
            raw_headers=headers,
            raw_body=raw_text,
            outcome=outcome,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(
            "wechat notify: transaction %s already recorded; not recording %s again",
            transaction_id,
            outcome,
        )
        return False
    return True


def _dispatch_finalize(booking_id: str) -> None:
    """Run the post-payment work (calendar confirm + email) off the request path.

    Imported lazily so the notify endpoint keeps working while ``app.tasks`` is
    still being written, and guarded so a failure here can never turn a
    successful payment into a failed response.
    """
    try:
        from app.tasks import finalize_paid_booking
    except Exception:  # noqa: BLE001 - the booking is paid regardless
        logger.exception(
            "wechat notify: finalize_paid_booking unavailable; booking %s is paid "
            "but calendar/email were not dispatched",
            booking_id,
        )
        return
    try:
        finalize_paid_booking(booking_id)
    except Exception:  # noqa: BLE001 - never mask a completed payment
        logger.exception("wechat notify: finalize_paid_booking failed for %s", booking_id)


def _handle_notification(
    db: Session,
    notification,
    headers: dict[str, str],
    raw_text: str,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    booking = db.scalar(
        select(Booking).where(Booking.out_trade_no == notification.out_trade_no)
    )
    if booking is None:
        logger.error(
            "wechat notify: unknown out_trade_no %s (transaction %s) — answering and stopping",
            notification.out_trade_no,
            notification.transaction_id,
        )
        _record(
            db,
            out_trade_no=notification.out_trade_no,
            transaction_id=notification.transaction_id,
            headers=headers,
            raw_text=raw_text,
            outcome="unknown_order",
        )
        return _ack("FAIL", "unknown order")

    # Replay guard: transaction_id is unique and WeChat resends up to 15 times.
    existing = db.scalar(
        select(PaymentEvent).where(
            PaymentEvent.transaction_id == notification.transaction_id
        )
    )
    if existing is not None:
        logger.info(
            "wechat notify: replay of transaction %s for %s; no second side effect",
            notification.transaction_id,
            notification.out_trade_no,
        )
        return _ack("SUCCESS", "成功")

    event = PaymentEvent(
        out_trade_no=notification.out_trade_no,
        transaction_id=notification.transaction_id,
        kind="notify",
        raw_headers=headers,
        raw_body=raw_text,
        outcome="",
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent delivery of the same transaction won the insert race.
        db.rollback()
        logger.info(
            "wechat notify: concurrent replay of transaction %s; ignoring",
            notification.transaction_id,
        )
        return _ack("SUCCESS", "成功")

    if notification.amount_fen != booking.amount_fen:
        event.outcome = "amount_mismatch"
        db.commit()
        logger.error(
            "wechat notify: AMOUNT MISMATCH for %s — expected %d fen, got %d fen; "
            "booking left pending_payment",
            booking.reference,
            booking.amount_fen,
            notification.amount_fen,
        )
        return _ack("FAIL", "amount mismatch")

    if notification.trade_state != "SUCCESS":
        event.outcome = f"trade_state={notification.trade_state}"
        db.commit()
        logger.error(
            "wechat notify: trade_state %s for %s is not SUCCESS; booking left pending_payment",
            notification.trade_state,
            booking.reference,
        )
        return _ack("FAIL", "trade state not success")

    # Settle the payment.  `honour_late_payment` also handles the case where the
    # hold expired or was cancelled before the callback landed: a verified payment
    # must never be silently dropped.
    try:
        honour_late_payment(
            db,
            booking,
            transaction_id=notification.transaction_id,
            paid_at=_coerce_datetime(notification.success_time),
        )
    except PaymentConflict as conflict:
        event.outcome = "paid_conflict"
        db.commit()
        logger.error(
            "wechat notify: CONFLICT for %s — %s. The money is real and the slot is "
            "gone; a refund is required. Answering FAIL so this is never recorded as "
            "a success.",
            booking.reference,
            conflict,
        )
        return _ack("FAIL", "booking no longer holds this slot; refund required")

    # SUCCESS must imply the booking really is paid.  If this ever fires, the
    # handler and the service have drifted apart and WeChat was about to be told a
    # lie — which is how a paid customer ends up with no booking and no signal.
    if booking.status is not BookingStatus.PAID:
        event.outcome = "not_paid"
        db.commit()
        logger.error(
            "wechat notify: booking %s is %s after settlement — refusing to ack SUCCESS",
            booking.reference,
            booking.status.value,
        )
        return _ack("FAIL", "booking not marked paid")

    event.outcome = "paid"
    db.commit()
    logger.info(
        "wechat notify: booking %s paid (transaction %s)",
        booking.reference,
        notification.transaction_id,
    )

    # Calendar confirm + email can exceed WeChat's 5 s budget — never inline.
    background_tasks.add_task(_dispatch_finalize, booking.id)
    return _ack("SUCCESS", "成功")


@router.post("/wechat/notify")
async def wechat_notify(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    payments: PaymentGateway = Depends(get_payment_gateway),
) -> JSONResponse:
    # Read the raw body exactly once — the signature is over these bytes.
    raw_body = await request.body()

    try:
        notification = payments.parse_notification(request.headers, raw_body)
    except PaymentSignatureError:
        logger.error(
            "wechat notify: signature verification FAILED (serial=%s); nothing persisted",
            request.headers.get("wechatpay-serial"),
        )
        return _ack("FAIL", "invalid signature", status_code=401)
    except Exception:  # noqa: BLE001 - fail closed, persist nothing
        logger.exception("wechat notify: could not parse notification; nothing persisted")
        return _ack("FAIL", "invalid notification")

    headers = dict(request.headers)
    raw_text = raw_body.decode("utf-8", errors="replace")
    try:
        return _handle_notification(db, notification, headers, raw_text, background_tasks)
    except Exception:  # noqa: BLE001
        # A 500 is retryable to WeChat and a retry storm is worse than a logged
        # failure, so answer 200/FAIL and keep the stack trace in our logs.
        logger.exception(
            "wechat notify: unexpected failure for %s", notification.out_trade_no
        )
        db.rollback()
        return _ack("FAIL", "internal error")
