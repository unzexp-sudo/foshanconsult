"""Payment port.  Frozen by docs/MODULE_CONTRACT.md §8."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChargeRequest:
    out_trade_no: str
    amount_fen: int
    description: str
    expires_at: datetime  # aware UTC


@dataclass(frozen=True)
class ChargeResult:
    code_url: str
    provider_order_id: str | None = None


@dataclass(frozen=True)
class PaymentNotification:
    out_trade_no: str
    transaction_id: str
    amount_fen: int
    trade_state: str  # "SUCCESS" | "CLOSED" | "NOTPAY" | ...
    success_time: datetime | None


class PaymentGatewayError(Exception):
    """Any failure talking to the payment provider."""


class PaymentSignatureError(PaymentGatewayError):
    """The callback signature did not verify.  Never trust the payload."""


class PaymentAmountMismatch(PaymentGatewayError):
    """The provider's amount does not match the amount we recorded."""


@runtime_checkable
class PaymentGateway(Protocol):
    def create_charge(self, req: ChargeRequest) -> ChargeResult: ...

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentNotification:
        """Verify the signature, then decrypt.  Raise PaymentSignatureError on a bad
        signature — never return unverified data."""
        ...

    def query_order(self, out_trade_no: str) -> PaymentNotification | None: ...

    def close_order(self, out_trade_no: str) -> None: ...
