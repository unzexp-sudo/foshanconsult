"""WeChat Pay signature and decryption tests (M2, contract §8 / BUILD_PLAN §6).

Every test here is offline.  The callbacks are built by ``tests.fakes``'s
``make_wechat_notify``, which produces a *genuinely* RSA-SHA256 signed and
*AES-256-GCM* encrypted body from a throwaway keypair — so these assertions are
about the real crypto, not a mock that would accept anything.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.adapters.payments_wechat import WechatPayGateway
from app.ports.payments import (
    ChargeRequest,
    PaymentGatewayError,
    PaymentSignatureError,
)
from tests.fakes import make_wechat_keys, make_wechat_notify, wechat_settings


@pytest.fixture
def keys(tmp_path: Path):
    return make_wechat_keys(tmp_path)


@pytest.fixture
def gateway(keys) -> WechatPayGateway:
    return WechatPayGateway(config=wechat_settings(keys))


def _resign(keys, headers: dict[str, str], envelope: dict) -> tuple[dict[str, str], bytes]:
    """Re-sign an (already mutated) envelope with the platform private key."""
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    private_key = serialization.load_pem_private_key(
        keys.private_key_pem.encode("utf-8"), password=None
    )
    message = (
        f"{headers['Wechatpay-Timestamp']}\n{headers['Wechatpay-Nonce']}\n"
        f"{body.decode('utf-8')}\n"
    ).encode()
    signature = base64.b64encode(
        private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return {**headers, "Wechatpay-Signature": signature}, body


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_notification_verifies_and_decrypts(gateway, keys):
    transaction_id = "4200001234567890abcdef"
    headers, body = make_wechat_notify(
        keys=keys,
        out_trade_no="BKVALID01",
        amount_fen=50000,
        transaction_id=transaction_id,
    )

    notification = gateway.parse_notification(headers, body)

    assert notification.out_trade_no == "BKVALID01"
    assert notification.transaction_id == transaction_id
    assert notification.amount_fen == 50000
    assert notification.trade_state == "SUCCESS"
    assert notification.success_time is not None
    assert notification.success_time.tzinfo is not None


# ---------------------------------------------------------------------------
# Fail-closed: every tampering switch must raise, and nothing unverified may
# leave the method.
# ---------------------------------------------------------------------------


def test_tampered_body_is_rejected_and_never_decrypted(gateway, keys, monkeypatch):
    reached: list[bool] = []

    def _spy(resource):
        reached.append(True)
        raise AssertionError("decryption must not be reached for a tampered body")

    monkeypatch.setattr(gateway, "_decrypt_resource", _spy)

    headers, body = make_wechat_notify(
        keys=keys, out_trade_no="BKTAMPER1", amount_fen=50000, tamper_body=True
    )

    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)
    assert reached == [], "AES-GCM was reached before the signature was verified"


def test_tampered_signature_is_rejected(gateway, keys):
    headers, body = make_wechat_notify(
        keys=keys, out_trade_no="BKSIG001", amount_fen=50000, tamper_signature=True
    )
    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)


def test_stale_timestamp_is_rejected_even_with_a_valid_signature(gateway, keys):
    """Freshness is checked independently of the signature."""
    headers, body = make_wechat_notify(
        keys=keys,
        out_trade_no="BKSTALE01",
        amount_fen=50000,
        timestamp=int(time.time()) - 600,
    )
    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)


def test_missing_signature_header_is_rejected(gateway, keys):
    headers, body = make_wechat_notify(
        keys=keys, out_trade_no="BKNOSIG01", amount_fen=50000, omit_signature=True
    )
    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)


def test_wrong_wechatpay_serial_is_rejected(gateway, keys):
    headers, body = make_wechat_notify(
        keys=keys, out_trade_no="BKSERIAL1", amount_fen=50000, bad_serial=True
    )
    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)


def test_body_signed_by_a_different_keypair_is_rejected(gateway, keys, tmp_path):
    other = make_wechat_keys(tmp_path / "other-platform")
    headers, body = make_wechat_notify(
        keys=other, out_trade_no="BKFOREIGN1", amount_fen=50000
    )
    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(headers, body)


def test_associated_data_mismatch_fails_instead_of_returning_garbage(gateway, keys):
    headers, body = make_wechat_notify(
        keys=keys, out_trade_no="BKAAD001", amount_fen=50000
    )
    mutated = json.loads(body.decode("utf-8"))
    # The ciphertext stays valid; the AAD the receiver will use no longer matches.
    mutated["resource"]["associated_data"] = "not-transaction"
    new_headers, new_body = _resign(keys, headers, mutated)

    with pytest.raises(PaymentSignatureError):
        gateway.parse_notification(new_headers, new_body)


# ---------------------------------------------------------------------------
# Outbound request signing
# ---------------------------------------------------------------------------


def test_create_charge_request_signature_verifies(keys):
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"code_url": "weixin://wxpay/bizpayurl?pr=ABC123"})

    config = wechat_settings(keys)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = WechatPayGateway(config=config, client=client)

    result = gateway.create_charge(
        ChargeRequest(
            out_trade_no="BKCHARGE1",
            amount_fen=50000,
            description="1-1 consultation",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )

    assert result.code_url == "weixin://wxpay/bizpayurl?pr=ABC123"

    request = captured["request"]
    assert request.method == "POST"
    assert request.url.path == "/v3/pay/transactions/native"

    payload = json.loads(request.content.decode("utf-8"))
    assert payload["out_trade_no"] == "BKCHARGE1"
    assert payload["amount"] == {"total": 50000, "currency": "CNY"}
    assert payload["notify_url"] == config.wechat_notify_url
    assert payload["time_expire"].endswith("+00:00")  # RFC3339 with an offset

    authorization = request.headers["Authorization"]
    assert authorization.startswith("WECHATPAY2-SHA256-RSA2048 ")
    fields = dict(re.findall(r'(\w+)="([^"]*)"', authorization))
    assert fields["mchid"] == config.wechat_mch_id
    assert fields["serial_no"] == keys.serial_no

    # Verify the signature ourselves against the merchant public key.
    message = (
        f"POST\n/v3/pay/transactions/native\n{fields['timestamp']}\n"
        f"{fields['nonce_str']}\n{request.content.decode('utf-8')}\n"
    ).encode()
    merchant_public = serialization.load_pem_private_key(
        keys.private_key_path.read_bytes(), password=None
    ).public_key()
    merchant_public.verify(
        base64.b64decode(fields["signature"]), message, padding.PKCS1v15(), hashes.SHA256()
    )


# ---------------------------------------------------------------------------
# close_order
# ---------------------------------------------------------------------------


def test_close_order_treats_already_closed_as_success(keys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"code": "ORDER_CLOSED", "message": "订单已关闭"}
        )

    gateway = WechatPayGateway(
        config=wechat_settings(keys),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    gateway.close_order("BKCLOSED1")  # must not raise


def test_close_order_raises_on_a_real_error(keys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "SYSTEM_ERROR", "message": "系统异常"})

    gateway = WechatPayGateway(
        config=wechat_settings(keys),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(PaymentGatewayError):
        gateway.close_order("BKCLOSED2")
