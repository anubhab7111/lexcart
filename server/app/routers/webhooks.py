"""
Razorpay webhook receiver — the reconciliation safety net.

The in-app checkout flows (web/concierge/agent) also verify payment via a
client-supplied signature at /verify or /pay-mock, but that call can be
lost (browser closed, network drop) even though the gateway captured the
money. The webhook is the source of truth Razorpay itself pushes, so
payment.captured here closes that gap idempotently. Campaign payment links
are paid outside any in-app cart entirely (whoever clicks the shared
link), so payment_link.paid is not a safety net but the *only* place that
order and booking get created.

Every event is verified against RAZORPAY_WEBHOOK_SECRET (HMAC-SHA256 over
the raw body, per Razorpay's spec) before anything is trusted — a rejected
signature is itself audited so a forged-webhook attempt leaves a trail.
"""

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.orders import (
    CartError,
    OrderAlreadyProcessed,
    PaymentVerificationError,
    confirm_campaign_link_payment,
    confirm_payment,
)
from app.db.engine import get_session
from app.db.models import Campaign, Order, OrderStatus
from app.services.razorpay_gateway import get_gateway

router = APIRouter(tags=["webhooks"])

# Service account attributed with bookings made by whoever pays a shared
# campaign payment link — there's no signed-in user in that flow, only a
# gateway event. Seeded in app/db/init_db.py alongside agent-buyer.
CAMPAIGN_BUYER_USER_ID = "campaign-buyer"


@router.post("/api/webhooks/razorpay/_mock-sign")
async def mock_sign_webhook(request: Request):
    """Demo/test-only: fabricate a valid webhook signature for an
    arbitrary event body, exactly as demo/evaluate.py's mock-pay flow
    fabricates payment signatures. 403s when real keys are configured —
    there is no way to forge a signature against a real webhook secret."""
    gateway = get_gateway()
    if not gateway.is_mock:
        return JSONResponse(
            status_code=403,
            content={"message": "Signature fabrication is only available on the mock gateway."},
        )
    raw = await request.body()
    return {"signature": gateway.sign_webhook_payload(raw)}


@router.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request, session: Session = Depends(get_session)):
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    gateway = get_gateway()

    if not signature or not gateway.verify_webhook_signature(raw, signature):
        log_action(
            session, "gateway", "webhook_rejected",
            rationale="X-Razorpay-Signature missing or did not verify; event discarded, "
            "nothing was trusted from this request",
            detail={"bodyPreview": raw[:200].decode("utf-8", "replace")},
        )
        return JSONResponse(status_code=400, content={"message": "Invalid webhook signature"})

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"message": "Malformed JSON body"})

    event_type = event.get("event", "")

    if event_type == "payment.captured":
        payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        payment_id = payment_entity.get("id", "")
        order = (
            session.exec(select(Order).where(Order.razorpay_order_id == razorpay_order_id)).first()
            if razorpay_order_id
            else None
        )
        if order is None:
            log_action(
                session, "gateway", "webhook_ignored",
                rationale=f"payment.captured for unrecognised order {razorpay_order_id!r}",
                detail={"paymentId": payment_id},
            )
            return {"status": "ignored", "reason": "unknown order"}
        if order.status != OrderStatus.created:
            return {"status": "already_processed", "orderStatus": order.status.value}
        try:
            confirm_payment(
                session, order, payment_id, "", actor="gateway", actor_ref="webhook",
                pre_verified=True,
            )
        except OrderAlreadyProcessed as e:
            # A client-side /verify (or agent pay-mock) call beat the
            # webhook to this order under the row lock -- that's the normal
            # case this "already_processed" check above was trying to
            # short-circuit anyway, just caught atomically instead of via
            # a racy pre-read.
            return {"status": "already_processed", "orderStatus": e.order.status.value}
        except PaymentVerificationError as e:
            return {"status": "error", "message": str(e)}
        return {"status": "processed"}

    if event_type == "payment_link.paid":
        link_entity = event.get("payload", {}).get("payment_link", {}).get("entity", {})
        payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        link_id = link_entity.get("id")
        payment_id = payment_entity.get("id", "")
        campaign = (
            session.exec(select(Campaign).where(Campaign.payment_link_id == link_id)).first()
            if link_id
            else None
        )
        if campaign is None:
            log_action(
                session, "gateway", "webhook_ignored",
                rationale=f"payment_link.paid for unrecognised link {link_id!r}",
                detail={"paymentId": payment_id},
            )
            return {"status": "ignored", "reason": "unknown payment link"}
        try:
            confirm_campaign_link_payment(
                session, campaign, payment_id, CAMPAIGN_BUYER_USER_ID,
                actor="gateway", actor_ref="webhook",
            )
        except CartError as e:
            return {"status": "error", "message": str(e)}
        return {"status": "processed"}

    return {"status": "ignored", "event": event_type or "unknown"}
