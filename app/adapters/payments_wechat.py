"""WeChat Pay Native (扫码支付) adapter — contract §8.

Money is only real when :meth:`WechatPayGateway.parse_notification` says so, so
that method is deliberately paranoid and fails closed at every step:

1. every ``Wechatpay-*`` header is present
2. ``abs(now - timestamp) <= 300`` — checked *before* the body is touched
3. RSA-SHA256 over ``f"{timestamp}\\n{nonce}\\n{body}\\n"`` using the platform
   public key, and ``Wechatpay-Serial`` must match our configured public key id
4. only then AES-256-GCM decrypt the ``resource`` with the APIv3 key

Any failure raises :class:`PaymentSignatureError`; unverified data never leaves
the method.  The APIv3 key, the merchant private key and full signatures are
never logged.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings, settings
from app.ports.payments import (
    ChargeRequest,
    ChargeResult,
    PaymentGatewayError,
    PaymentNotification,
    PaymentSignatureError,
)

WECHAT_BASE_URL = "https://api.mch.weixin.qq.com"
NATIVE_PATH = "/v3/pay/transactions/native"
SIGN_TYPE = "WECHATPAY2-SHA256-RSA2048"
MAX_SKEW_SECONDS = 300


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — HTTP header names are not case sensitive."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _rfc3339(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _looks_already_closed(text: str) -> bool:
    lowered = text.lower()
    return (
        "order_closed" in lowered
        or "orderclosed" in lowered
        or "already closed" in lowered
        or "已关闭" in text
    )


def _notification_from_transaction(data: Mapping) -> PaymentNotification:
    amount = data.get("amount") or {}
    return PaymentNotification(
        out_trade_no=str(data["out_trade_no"]),
        transaction_id=str(data.get("transaction_id") or ""),
        amount_fen=int(amount.get("total", 0)),
        trade_state=str(data.get("trade_state") or ""),
        success_time=_parse_rfc3339(data.get("success_time")),
    )


class WechatPayGateway:
    """WeChat Pay Native adapter.

    ``config`` defaults to the module-level :data:`app.config.settings` so
    ``app.deps.get_payment_gateway`` can build it with no arguments; tests inject
    ``wechat_settings(keys)``.  ``client`` is an injection seam for
    ``httpx.MockTransport`` — production always builds its own client.
    """

    def __init__(
        self,
        config: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or settings
        self._client = client
        self._private_key: rsa.RSAPrivateKey | None = None
        self._public_key: rsa.RSAPublicKey | None = None

    # -- key loading --------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            pem = Path(self.config.wechat_private_key_path).read_bytes()
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise PaymentGatewayError("wechat merchant key is not an RSA private key")
            self._private_key = key
        return self._private_key

    def _load_public_key(self) -> rsa.RSAPublicKey:
        if self._public_key is None:
            pem = Path(self.config.wechat_public_key_path).read_bytes()
            key = serialization.load_pem_public_key(pem)
            if not isinstance(key, rsa.RSAPublicKey):
                raise PaymentGatewayError("wechat platform key is not an RSA public key")
            self._public_key = key
        return self._public_key

    def _signed_headers(self, method: str, url_path: str, body_text: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body_text}\n".encode()
        signature = base64.b64encode(
            self._load_private_key().sign(message, padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii")
        authorization = (
            f"{SIGN_TYPE} "
            f'mchid="{self.config.wechat_mch_id}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{self.config.wechat_cert_serial_no}"'
        )
        return {
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "zhituoyuan-booking/0.1",
        }

    # -- outbound -----------------------------------------------------------

    def create_charge(self, req: ChargeRequest) -> ChargeResult:
        payload = {
            "appid": self.config.wechat_app_id,
            "mchid": self.config.wechat_mch_id,
            "description": req.description,
            "out_trade_no": req.out_trade_no,
            "time_expire": _rfc3339(req.expires_at),
            "notify_url": self.config.wechat_notify_url,
            "amount": {"total": req.amount_fen, "currency": "CNY"},
        }
        body_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = self._signed_headers("POST", NATIVE_PATH, body_text)
        response = self._http().post(
            WECHAT_BASE_URL + NATIVE_PATH,
            content=body_text.encode("utf-8"),
            headers=headers,
        )
        if response.status_code >= 400:
            raise PaymentGatewayError(f"wechat create_charge failed: {response.status_code}")
        try:
            data = response.json()
            code_url = data["code_url"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PaymentGatewayError(
                "wechat create_charge returned an unusable response"
            ) from exc
        return ChargeResult(code_url=str(code_url), provider_order_id=data.get("prepay_id"))

    def query_order(self, out_trade_no: str) -> PaymentNotification | None:
        url_path = (
            f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}"
            f"?mchid={self.config.wechat_mch_id}"
        )
        headers = self._signed_headers("GET", url_path, "")
        response = self._http().get(WECHAT_BASE_URL + url_path, headers=headers)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PaymentGatewayError(f"wechat query_order failed: {response.status_code}")
        try:
            return _notification_from_transaction(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise PaymentGatewayError("wechat query_order returned an unusable response") from exc

    def close_order(self, out_trade_no: str) -> None:
        url_path = f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}/close"
        body_text = json.dumps({"mchid": self.config.wechat_mch_id}, separators=(",", ":"))
        headers = self._signed_headers("POST", url_path, body_text)
        response = self._http().post(
            WECHAT_BASE_URL + url_path,
            content=body_text.encode("utf-8"),
            headers=headers,
        )
        if response.status_code in (200, 204):
            return
        # Closing an order that is already closed is not a failure — the sweeper
        # calls this blindly and must not raise on a duplicate close.
        if response.status_code == 400 and _looks_already_closed(response.text):
            return
        raise PaymentGatewayError(f"wechat close_order failed: {response.status_code}")

    # -- inbound: the critical method --------------------------------------

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentNotification:
        """Verify then decrypt.  Fail closed; never return unverified data."""
        # 1. required headers
        timestamp = _header(headers, "Wechatpay-Timestamp")
        nonce = _header(headers, "Wechatpay-Nonce")
        signature = _header(headers, "Wechatpay-Signature")
        serial = _header(headers, "Wechatpay-Serial")
        if not timestamp or not nonce or not signature or not serial:
            raise PaymentSignatureError("missing WeChat Pay signature headers")

        # 2. freshness — before verifying and before touching the body
        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise PaymentSignatureError("malformed Wechatpay-Timestamp") from exc
        if abs(time.time() - timestamp_value) > MAX_SKEW_SECONDS:
            raise PaymentSignatureError("stale Wechatpay-Timestamp")

        # 3. signature + serial
        if not hmac.compare_digest(serial, self.config.wechat_public_key_id):
            raise PaymentSignatureError("unknown Wechatpay-Serial")

        try:
            body_text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PaymentSignatureError("notification body is not valid UTF-8") from exc

        message = f"{timestamp}\n{nonce}\n{body_text}\n".encode()
        try:
            signature_bytes = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaymentSignatureError("malformed Wechatpay-Signature") from exc
        try:
            self._load_public_key().verify(
                signature_bytes, message, padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature as exc:
            raise PaymentSignatureError("Wechatpay-Signature did not verify") from exc

        # 4 + 5. decrypt, then parse — still inside the fail-closed boundary
        try:
            envelope = json.loads(body_text)
            resource = envelope["resource"]
            if not isinstance(resource, dict):
                raise TypeError("resource is not an object")
            algorithm = resource.get("algorithm")
            if algorithm not in (None, "AEAD_AES_256_GCM"):
                raise ValueError(f"unsupported resource algorithm {algorithm!r}")
            plaintext = self._decrypt_resource(resource)
            transaction = json.loads(plaintext.decode("utf-8"))
            amount = transaction["amount"]
            return PaymentNotification(
                out_trade_no=str(transaction["out_trade_no"]),
                transaction_id=str(transaction["transaction_id"]),
                amount_fen=int(amount["total"]),
                trade_state=str(transaction.get("trade_state") or ""),
                success_time=_parse_rfc3339(transaction.get("success_time")),
            )
        except PaymentSignatureError:
            raise
        except Exception as exc:  # noqa: BLE001 - anything here means "do not trust it"
            raise PaymentSignatureError(
                "could not decrypt or parse the notification resource"
            ) from exc

    def _decrypt_resource(self, resource: Mapping[str, object]) -> bytes:
        """AES-256-GCM open.  Kept separate so a test can prove it is never reached
        when the signature does not verify."""
        nonce = resource.get("nonce")
        ciphertext = resource.get("ciphertext")
        if not isinstance(nonce, str) or not isinstance(ciphertext, str):
            raise PaymentSignatureError("notification resource is missing nonce/ciphertext")
        associated_data = resource.get("associated_data") or ""
        if not isinstance(associated_data, str):
            raise PaymentSignatureError("notification associated_data is not a string")
        try:
            raw = base64.b64decode(ciphertext, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaymentSignatureError("notification ciphertext is not valid base64") from exc
        aesgcm = AESGCM(self.config.wechat_api_v3_key.encode("utf-8"))
        return aesgcm.decrypt(
            nonce.encode("utf-8"), raw, associated_data.encode("utf-8")
        )
