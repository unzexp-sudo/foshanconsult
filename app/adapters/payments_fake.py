"""In-process fake WeChat Pay gateway — contract §2 / §8.

``PAYMENT_GATEWAY=fake`` keeps local dev and the entire test suite offline.  It
fails closed in the same *shape* as the real adapter: a bad HMAC raises
:class:`PaymentSignatureError` before a single field of the payload is trusted.

Self-contained on purpose — it must not import from ``tests/``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.ports.payments import (
    ChargeRequest,
    ChargeResult,
    PaymentGatewayError,
    PaymentNotification,
    PaymentSignatureError,
)

SIGNATURE_HEADER = "X-Fake-Signature"


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass
class FakePaymentGateway:
    """HMAC-signed stand-in for WeChat Pay.

    Constructible with no arguments, which is how ``app.deps`` builds it.
    """

    secret_key: str = "dev-only-change-me"
    charges: list[ChargeRequest] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    orders: dict[str, PaymentNotification] = field(default_factory=dict)
    fail_create: bool = False

    def create_charge(self, req: ChargeRequest) -> ChargeResult:
        if self.fail_create:
            raise PaymentGatewayError("fake payment gateway configured to fail")
        self.charges.append(req)
        return ChargeResult(
            code_url=f"weixin://wxpay/bizpayurl?pr=FAKE{req.out_trade_no}",
            provider_order_id=f"fake-{req.out_trade_no}",
        )

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentNotification:
        lowered = {key.lower(): value for key, value in headers.items()}
        provided = lowered.get(SIGNATURE_HEADER.lower(), "")
        expected = hmac.new(
            self.secret_key.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(provided, expected):
            raise PaymentSignatureError("bad fake signature")

        try:
            payload = json.loads(body.decode("utf-8"))
            notification = PaymentNotification(
                out_trade_no=str(payload["out_trade_no"]),
                transaction_id=str(payload["transaction_id"]),
                amount_fen=int(payload["amount_fen"]),
                trade_state=str(payload.get("trade_state", "SUCCESS")),
                success_time=_parse_datetime(payload.get("success_time")),
            )
        except Exception as exc:  # noqa: BLE001 - a malformed payload is not trusted
            raise PaymentSignatureError("fake notification payload is malformed") from exc

        self.orders[notification.out_trade_no] = notification
        return notification

    def query_order(self, out_trade_no: str) -> PaymentNotification | None:
        return self.orders.get(out_trade_no)

    def close_order(self, out_trade_no: str) -> None:
        self.closed.append(out_trade_no)

    def build_notify(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        transaction_id: str | None = None,
        trade_state: str = "SUCCESS",
        success_time: datetime | None = None,
    ) -> tuple[dict[str, str], bytes]:
        """A valid, correctly HMAC-signed callback for this gateway."""
        body = json.dumps(
            {
                "out_trade_no": out_trade_no,
                "transaction_id": transaction_id or f"FAKE{uuid.uuid4().hex[:16]}",
                "amount_fen": amount_fen,
                "trade_state": trade_state,
                "success_time": (success_time or datetime.now(UTC)).isoformat(),
            }
        ).encode("utf-8")
        signature = hmac.new(
            self.secret_key.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return {SIGNATURE_HEADER: signature}, body
