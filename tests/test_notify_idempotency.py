"""Notify-endpoint idempotency tests (M2, contract §11 / BUILD_PLAN §6).

These drive the real HTTP endpoint with the **real** ``WechatPayGateway``,
injected through ``app.dependency_overrides``, using genuinely signed and
encrypted callbacks from ``tests.fakes.make_wechat_notify``.  No network.
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.adapters.payments_wechat import WechatPayGateway
from app.deps import get_payment_gateway
from app.main import app
from app.models import Booking, BookingStatus, PaymentEvent
from tests.fakes import make_wechat_notify, wechat_settings

NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
NOTIFY_URL = "/api/payments/wechat/notify"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def wechat_gateway(wechat_keys) -> WechatPayGateway:
    return WechatPayGateway(config=wechat_settings(wechat_keys))


@pytest.fixture
def notify_client(client, wechat_gateway):
    """The conftest ``client`` (fake calendar/email) with the real payment gateway."""
    app.dependency_overrides[get_payment_gateway] = lambda: wechat_gateway
    yield client
    app.dependency_overrides.pop(get_payment_gateway, None)


def make_booking(
    db,
    event_type,
    *,
    reference: str,
    amount_fen: int = 50000,
    status: BookingStatus = BookingStatus.PENDING_PAYMENT,
) -> Booking:
    slot_start = NOW + timedelta(hours=5)
    booking = Booking(
        id=uuid.uuid4().hex,
        reference=reference,
        event_type_id=event_type.id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=event_type.duration_minutes),
        status=status,
        expires_at=NOW + timedelta(minutes=10),
        amount_fen=amount_fen,
        currency="CNY",
        customer_name="Alice",
        customer_email="alice@example.com",
        out_trade_no=reference,
    )
    db.add(booking)
    db.commit()
    return booking


def reload_booking(db, reference: str) -> Booking:
    db.expire_all()
    row = db.scalar(select(Booking).where(Booking.reference == reference))
    assert row is not None
    return row


def post_notify(
    client,
    keys,
    *,
    out_trade_no: str,
    amount_fen: int,
    transaction_id: str | None = None,
    trade_state: str = "SUCCESS",
    success_time: datetime | None = None,
    **kwargs,
):
    headers, body = make_wechat_notify(
        keys=keys,
        out_trade_no=out_trade_no,
        amount_fen=amount_fen,
        transaction_id=transaction_id,
        trade_state=trade_state,
        success_time=success_time,
        **kwargs,
    )
    return client.post(NOTIFY_URL, content=body, headers=headers)


def payment_events(db) -> list[PaymentEvent]:
    return list(db.scalars(select(PaymentEvent)))


def new_transaction_id() -> str:
    return "4200001" + uuid.uuid4().hex[:14]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_callback_marks_booking_paid(notify_client, db_session, event_type, wechat_keys):
    make_booking(db_session, event_type, reference="BKPAID001")
    transaction_id = new_transaction_id()

    response = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKPAID001",
        amount_fen=50000,
        transaction_id=transaction_id,
    )

    assert response.status_code == 200
    assert response.json() == {"code": "SUCCESS", "message": "成功"}

    booking = reload_booking(db_session, "BKPAID001")
    assert booking.status is BookingStatus.PAID
    assert booking.provider_transaction_id == transaction_id
    assert booking.paid_at is not None


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_of_the_same_transaction_has_no_second_side_effect(
    notify_client, db_session, event_type, wechat_keys
):
    make_booking(db_session, event_type, reference="BKREPLAY1")
    transaction_id = new_transaction_id()

    first = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKREPLAY1",
        amount_fen=50000,
        transaction_id=transaction_id,
    )
    assert first.json()["code"] == "SUCCESS"

    paid_at_after_first = reload_booking(db_session, "BKREPLAY1").paid_at
    assert len(payment_events(db_session)) == 1

    # Same transaction_id, deliberately different success_time — it must be ignored.
    second = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKREPLAY1",
        amount_fen=50000,
        transaction_id=transaction_id,
        success_time=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert second.status_code == 200
    assert second.json()["code"] == "SUCCESS"
    assert len(payment_events(db_session)) == 1, "a replay must not add a second audit row"
    assert reload_booking(db_session, "BKREPLAY1").paid_at == paid_at_after_first


# ---------------------------------------------------------------------------
# Never mark paid on a mismatch
# ---------------------------------------------------------------------------


def test_amount_mismatch_leaves_booking_pending_and_records_an_event(
    notify_client, db_session, event_type, wechat_keys
):
    make_booking(db_session, event_type, reference="BKMISMATCH1", amount_fen=50000)

    response = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKMISMATCH1",
        amount_fen=1,  # attacker pays ¥0.01 for a ¥500 booking
        transaction_id=new_transaction_id(),
    )

    assert response.status_code == 200
    assert response.json()["code"] == "FAIL"

    booking = reload_booking(db_session, "BKMISMATCH1")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.paid_at is None
    assert booking.provider_transaction_id is None

    events = payment_events(db_session)
    assert len(events) == 1
    assert events[0].outcome == "amount_mismatch"


def test_closed_trade_state_does_not_mark_the_booking_paid(
    notify_client, db_session, event_type, wechat_keys
):
    make_booking(db_session, event_type, reference="BKCLOSED1")

    response = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKCLOSED1",
        amount_fen=50000,
        transaction_id=new_transaction_id(),
        trade_state="CLOSED",
    )

    assert response.status_code == 200
    assert response.json()["code"] == "FAIL"
    assert reload_booking(db_session, "BKCLOSED1").status is BookingStatus.PENDING_PAYMENT
    assert len(payment_events(db_session)) == 1


# ---------------------------------------------------------------------------
# Forged callbacks persist nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper", [{"tamper_signature": True}, {"tamper_body": True}]
)
def test_forged_callback_returns_401_and_persists_nothing(
    notify_client, db_session, event_type, wechat_keys, tamper
):
    make_booking(db_session, event_type, reference="BKFORGED1")

    headers, body = make_wechat_notify(
        keys=wechat_keys,
        out_trade_no="BKFORGED1",
        amount_fen=50000,
        transaction_id=new_transaction_id(),
        **tamper,
    )
    response = notify_client.post(NOTIFY_URL, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": "FAIL", "message": "invalid signature"}

    booking = reload_booking(db_session, "BKFORGED1")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.paid_at is None
    assert payment_events(db_session) == []


# ---------------------------------------------------------------------------
# Unknown order
# ---------------------------------------------------------------------------


def test_unknown_out_trade_no_does_not_500_and_creates_no_booking(
    notify_client, db_session, event_type, wechat_keys
):
    before = db_session.scalar(select(func.count()).select_from(Booking))

    response = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKUNKNOWN1",
        amount_fen=50000,
        transaction_id=new_transaction_id(),
    )

    assert response.status_code == 200
    assert response.json()["code"] == "FAIL"
    assert "unknown" in response.json()["message"]

    after = db_session.scalar(select(func.count()).select_from(Booking))
    assert after == before

    events = payment_events(db_session)
    assert len(events) == 1
    assert events[0].outcome == "unknown_order"


# ---------------------------------------------------------------------------
# Post-payment work is off the request path
# ---------------------------------------------------------------------------


def test_post_payment_work_is_dispatched_in_the_background(
    notify_client, db_session, event_type, wechat_keys, monkeypatch
):
    booking = make_booking(db_session, event_type, reference="BKBG0001")

    calls: list[str] = []
    stub = types.ModuleType("app.tasks")
    stub.finalize_paid_booking = lambda booking_id: calls.append(booking_id)
    monkeypatch.setitem(sys.modules, "app.tasks", stub)

    response = post_notify(
        notify_client,
        wechat_keys,
        out_trade_no="BKBG0001",
        amount_fen=50000,
        transaction_id=new_transaction_id(),
    )

    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"
    # TestClient runs background tasks before the call returns, so the dispatch
    # has happened by now — and it carried the booking id.
    assert calls == [booking.id]
