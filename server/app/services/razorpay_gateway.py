"""
Razorpay test-mode gateway with a mock fallback.

All money movement in the app goes through this one wrapper. When
RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are present in .env the real SDK is
used (test mode — keys starting rzp_test_); when absent, a deterministic
mock implements the same surface so the whole agentic-commerce flow
(orders, signature verification, payment links) runs end to end locally.
Mock artifacts are always labeled (ids prefixed order_MOCK / plink_MOCK)
and the simulate-payment path refuses to run against real keys.
"""

import hashlib
import hmac
import time
import uuid
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

from app.config import get_settings


def _derive_mock_secret(purpose: str) -> str:
    """Mock secrets derived from JWT_SECRET rather than hardcoded — a
    hardcoded mock signing key in the repo would let anyone forge mock
    payment signatures against a deployed demo instance."""
    from app.security import jwt_secret

    return hashlib.sha256(f"lexcart-mock-{purpose}|{jwt_secret()}".encode()).hexdigest()


class PaymentGatewayError(Exception):
    pass


class RazorpayGateway:
    def __init__(self, key_id: str, key_secret: str):
        self.key_id = key_id
        self.key_secret = key_secret
        self.is_mock = not (key_id and key_secret)
        self._client = None
        if not self.is_mock:
            import razorpay

            self._client = razorpay.Client(auth=(key_id, key_secret))
            self._client.set_app_details({"title": "LexCart", "version": "1.0"})

    @property
    def public_key_id(self) -> str:
        """Key id the browser checkout needs; a sentinel in mock mode."""
        return self.key_id if not self.is_mock else "rzp_test_mock"

    def _signing_secret(self) -> str:
        return _derive_mock_secret("gateway-signature") if self.is_mock else self.key_secret

    def _webhook_secret(self) -> str:
        """Independent of the API key/secret — Razorpay signs webhook
        payloads with a separate secret set in the dashboard's Webhooks
        page. Falls back to a JWT_SECRET-derived mock secret so the
        webhook flow is exercisable without real keys."""
        configured = get_settings().razorpay_webhook_secret
        return configured or _derive_mock_secret("webhook-signature")

    def create_order(
        self,
        amount_inr: int,
        receipt: str,
        notes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a Razorpay Order. Amount is rupees; Razorpay wants paise."""
        amount_paise = int(amount_inr) * 100
        if self.is_mock:
            return {
                "id": f"order_MOCK{uuid.uuid4().hex[:14]}",
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "status": "created",
                "notes": notes or {},
                "created_at": int(time.time()),
                "mock": True,
            }
        try:
            order = self._client.order.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": receipt,
                    "notes": notes or {},
                }
            )
            order["mock"] = False
            return order
        except Exception as e:
            raise PaymentGatewayError(f"Razorpay order creation failed: {e}") from e

    def verify_payment_signature(
        self, order_id: str, payment_id: str, signature: str
    ) -> bool:
        """HMAC-SHA256 check that the payment callback really came from the
        gateway (or from our mock signer) — the client is never trusted."""
        expected = hmac.new(
            self._signing_secret().encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def mock_pay(self, order_id: str, fail: bool = False) -> Tuple[str, str]:
        """Simulate the gateway completing a payment for an order (mock mode
        only). Returns (payment_id, signature) exactly as checkout.js would
        hand back. `fail=True` returns a deliberately bad signature so the
        declined-payment path can be exercised."""
        if not self.is_mock:
            raise PaymentGatewayError(
                "simulate-payment is only available in mock mode; real keys are configured"
            )
        payment_id = f"pay_MOCK{uuid.uuid4().hex[:14]}"
        if fail:
            return payment_id, "invalid-signature-simulated-failure"
        signature = hmac.new(
            self._signing_secret().encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return payment_id, signature

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """HMAC-SHA256 over the exact raw request body, per Razorpay's
        webhook spec — never re-serialize the parsed JSON for this check,
        formatting differences would break a legitimate signature."""
        expected = hmac.new(self._webhook_secret().encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def sign_webhook_payload(self, raw_body: bytes) -> str:
        """Only meaningful in mock mode: lets a demo/test client fabricate a
        correctly-signed webhook event without a real Razorpay dashboard."""
        if not self.is_mock:
            raise PaymentGatewayError("cannot fabricate webhook signatures against real keys")
        return hmac.new(self._webhook_secret().encode(), raw_body, hashlib.sha256).hexdigest()

    def refund_payment(
        self, payment_id: str, amount_inr: int, notes: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        amount_paise = int(amount_inr) * 100
        if self.is_mock:
            return {
                "id": f"rfnd_MOCK{uuid.uuid4().hex[:14]}",
                "payment_id": payment_id,
                "amount": amount_paise,
                "currency": "INR",
                "status": "processed",
                "notes": notes or {},
                "mock": True,
            }
        try:
            refund = self._client.payment.refund(
                payment_id, {"amount": amount_paise, "notes": notes or {}}
            )
            refund["mock"] = False
            return refund
        except Exception as e:
            raise PaymentGatewayError(f"Razorpay refund failed: {e}") from e

    def create_payment_link(
        self,
        amount_inr: int,
        description: str,
        reference_id: str,
        notes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        amount_paise = int(amount_inr) * 100
        if self.is_mock:
            link_id = f"plink_MOCK{uuid.uuid4().hex[:12]}"
            return {
                "id": link_id,
                "short_url": f"https://rzp.io/mock/{link_id}",
                "amount": amount_paise,
                "currency": "INR",
                "description": description,
                "reference_id": reference_id,
                "status": "created",
                "notes": notes or {},
                "mock": True,
            }
        try:
            link = self._client.payment_link.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "description": description,
                    "reference_id": reference_id,
                    "notes": notes or {},
                }
            )
            link["mock"] = False
            return link
        except Exception as e:
            raise PaymentGatewayError(f"Razorpay payment link failed: {e}") from e


@lru_cache()
def get_gateway() -> RazorpayGateway:
    settings = get_settings()
    return RazorpayGateway(settings.razorpay_key_id, settings.razorpay_key_secret)
