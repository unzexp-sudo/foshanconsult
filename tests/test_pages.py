"""Server-rendered page tests (owner M5).

The two invariants that matter most on the payment surface:

* the WeChat ``code_url`` never reaches the HTML — not as text, not as an
  attribute, not in JS;
* the QR appears only for a live, unexpired ``pending_payment`` hold.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, EventType

# A stand-in for the WeChat Native payment token.  If this string ever shows up
# in a response body, the payment token has leaked.
CODE_URL = "weixin://wxpay/bizpayurl?pr=PAGETEST0001"


def _make_booking(
    db: Session,
    event_type: EventType,
    *,
    status: BookingStatus,
    reference: str,
    expires_in_minutes: int = 10,
    paid: bool = False,
) -> Booking:
    now = datetime.now(UTC)
    booking = Booking(
        id=uuid.uuid4().hex,
        reference=reference,
        event_type_id=event_type.id,
        slot_start=now + timedelta(days=3),
        slot_end=now + timedelta(days=3, minutes=event_type.duration_minutes),
        status=status,
        expires_at=now + timedelta(minutes=expires_in_minutes),
        amount_fen=event_type.price_fen,
        currency=event_type.currency,
        customer_name="测试用户",
        customer_email="customer@example.com",
        code_url=CODE_URL,
        out_trade_no=reference,
        paid_at=now if paid else None,
    )
    db.add(booking)
    db.commit()
    return booking


def test_booking_page_renders_event_type_title_and_price(
    event_type: EventType, client: TestClient
) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert event_type.title in response.text
    assert "¥500" in response.text
    assert CODE_URL not in response.text


def test_pending_page_embeds_qr_svg_but_not_the_code_url(
    event_type: EventType, db_session: Session, client: TestClient
) -> None:
    booking = _make_booking(
        db_session, event_type, status=BookingStatus.PENDING_PAYMENT, reference="BKPEND01"
    )

    response = client.get(f"/book/{booking.reference}")

    assert response.status_code == 200
    assert "<svg" in response.text
    assert "等待支付" in response.text
    # The token itself must never be in the body, in any form.
    assert CODE_URL not in response.text
    assert "weixin://" not in response.text


def test_paid_page_shows_confirmation_and_no_qr(
    event_type: EventType, db_session: Session, client: TestClient
) -> None:
    booking = _make_booking(
        db_session,
        event_type,
        status=BookingStatus.PAID,
        reference="BKPAID01",
        paid=True,
    )

    response = client.get(f"/book/{booking.reference}")

    assert response.status_code == 200
    assert "预约已确认" in response.text
    assert "<svg" not in response.text
    assert CODE_URL not in response.text


def test_expired_hold_renders_no_qr(
    event_type: EventType, db_session: Session, client: TestClient
) -> None:
    booking = _make_booking(
        db_session,
        event_type,
        status=BookingStatus.PENDING_PAYMENT,
        reference="BKEXPI01",
        expires_in_minutes=-5,
    )

    response = client.get(f"/book/{booking.reference}")

    assert response.status_code == 200
    assert "预约已失效" in response.text
    assert "<svg" not in response.text
    assert CODE_URL not in response.text


def test_unknown_reference_returns_friendly_404(client: TestClient) -> None:
    response = client.get("/book/BKNOSUCH")

    assert response.status_code == 404
    assert "未找到" in response.text
    assert "<svg" not in response.text
