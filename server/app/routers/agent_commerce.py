"""
Agent-to-agent commerce surface: makes the merchant transactable by an AI
buyer end to end.

Discovery is open (/.well-known/agent-catalog.json describes the catalog,
auth, and endpoints in machine-readable form); transacting requires an
X-Agent-Key. Money is bounded three ways: the global agent caps
(guardrails), the per-key daily limit, and the fact that an order only
becomes a booking after gateway-verified payment.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func as safunc
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.orders import (
    BoundsExceeded,
    CartError,
    OrderAlreadyProcessed,
    PaymentVerificationError,
    confirm_payment,
    create_order,
    price_cart,
    refund_order,
)
from app.config import get_settings
from app.db.engine import get_session
from app.db.models import (
    AgentApiKey,
    Lawyer,
    Order,
    OrderStatus,
    ServiceAddon,
)
from app.deps.errors import MessageHTTPException
from app.services.razorpay_gateway import PaymentGatewayError, get_gateway

router = APIRouter(tags=["agent-commerce"])

# Bookings need an owning user; external AI-buyer purchases are attributed
# to this seeded service account (see app/db/init_db.py).
AGENT_BUYER_USER_ID = "agent-buyer"


def get_agent_key(
    x_agent_key: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> AgentApiKey:
    if not x_agent_key:
        raise MessageHTTPException(status_code=401, detail="X-Agent-Key header required")
    key_hash = hashlib.sha256(x_agent_key.encode()).hexdigest()
    key = session.exec(select(AgentApiKey).where(AgentApiKey.key_hash == key_hash)).first()
    if key is None or not key.active:
        raise MessageHTTPException(status_code=401, detail="Invalid or inactive agent key")
    now = datetime.now(timezone.utc)
    # Throttled: this dependency runs on every agent-API call, so writing
    # last_used_at unconditionally is a DB write on the hot path for no
    # real benefit -- a minute's staleness on a "last used" display is
    # unnoticeable.
    if key.last_used_at is None or (now - key.last_used_at) > timedelta(seconds=60):
        key.last_used_at = now
        session.add(key)
        session.commit()
    return key


def _service_entry(lawyer: Lawyer, base_url: str) -> dict:
    """schema.org-flavoured Offer for one consultation service."""
    return {
        "@type": "Service",
        "serviceId": lawyer.id,
        "name": f"Legal consultation — {lawyer.name}",
        "category": lawyer.specialty,
        "description": lawyer.bio,
        "provider": {
            "name": lawyer.name,
            "areaServed": lawyer.location,
            "aggregateRating": {"ratingValue": lawyer.rating, "reviewCount": lawyer.cases},
            "knowsLanguage": lawyer.languages,
            "yearsExperience": lawyer.experience,
        },
        "offers": {
            "@type": "Offer",
            "price": lawyer.hourly_rate,
            "priceCurrency": "INR",
            "availability": lawyer.availability,
            "quoteUrl": f"{base_url}/api/agent/v1/quote",
        },
    }


@router.get("/.well-known/agent-catalog.json")
def well_known_catalog(session: Session = Depends(get_session)):
    """Open discovery document for AI buyers: who the merchant is, what it
    sells, how to authenticate, and where to transact."""
    settings = get_settings()
    base = settings.public_base_url
    lawyers = session.exec(select(Lawyer).order_by(Lawyer.id)).all()
    addons = session.exec(select(ServiceAddon).where(ServiceAddon.active == True)).all()  # noqa: E712
    return {
        "@context": "https://schema.org",
        "specVersion": "lexcart-agent-catalog/1.0",
        "merchant": {
            "@type": "LegalService",
            "name": "LexCart",
            "description": "Indian legal-services marketplace: book consultations with verified lawyers, with optional document review and drafting add-ons.",
            "currenciesAccepted": "INR",
            "paymentAccepted": "Razorpay (test mode)",
        },
        "authentication": {
            "type": "apiKey",
            "header": "X-Agent-Key",
            "note": "Contact the merchant for a key. Demo deployments seed one via init_db.",
        },
        "endpoints": {
            "catalog": f"{base}/api/agent/v1/catalog",
            "quote": f"{base}/api/agent/v1/quote",
            "orders": f"{base}/api/agent/v1/orders",
            "orderStatus": f"{base}/api/agent/v1/orders/{{orderId}}",
        },
        "bounds": {
            "maxOrderInr": settings.agent_max_order_inr,
            "note": "Orders above the cap are refused with an explanation; every action is audited.",
        },
        "semantics": {
            "idempotency": "POST /orders accepts buyerReference; a retried request with the "
            "same value returns the original order (idempotent: true) instead of duplicating it.",
        },
        "services": [_service_entry(lw, base) for lw in lawyers],
        "addons": [a.to_dict() for a in addons],
    }


@router.get("/api/agent/v1/catalog")
def agent_catalog(
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    lawyers = session.exec(select(Lawyer).order_by(Lawyer.id)).all()
    addons = session.exec(select(ServiceAddon).where(ServiceAddon.active == True)).all()  # noqa: E712
    return {
        "services": [_service_entry(lw, settings.public_base_url) for lw in lawyers],
        "addons": [a.to_dict() for a in addons],
    }


class QuoteRequest(BaseModel):
    serviceId: str
    addonIds: List[str] = []


@router.post("/api/agent/v1/quote")
def agent_quote(
    body: QuoteRequest,
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    """Firm, server-priced quote. No side effects beyond the audit row."""
    try:
        cart = price_cart(session, body.serviceId, body.addonIds)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    log_action(
        session, "ai_buyer", "quote_issued",
        actor_ref=key.id,
        rationale=f"agent '{key.name}' requested a quote for service {body.serviceId} "
        f"with addons {body.addonIds}",
        amount_inr=cart.total_inr,
        detail={"lineItems": cart.line_items()},
    )
    return {
        "serviceId": body.serviceId,
        "lineItems": cart.line_items(),
        "totalInr": cart.total_inr,
        "currency": "INR",
    }


class AgentOrderRequest(BaseModel):
    serviceId: str
    addonIds: List[str] = []
    buyerReference: str = ""


def _key_spend_24h(session: Session, key_id: str) -> int:
    """Paid orders in the last 24h plus still-open orders from the same 24h
    window, so a key can't dodge its daily limit by leaving orders unpaid
    past a short "still open" cutoff (mirrors guardrails.check_order_bounds's
    windowing, which has the same reasoning)."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return int(
        session.exec(
            select(safunc.coalesce(safunc.sum(Order.total_amount_inr), 0))
            .where(Order.agent_key_id == key_id)
            .where(Order.created_at >= since)
            .where(Order.status.in_([OrderStatus.paid, OrderStatus.created]))
        ).one()
    )


@router.post("/api/agent/v1/orders")
def agent_create_order(
    body: AgentOrderRequest,
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    # Idempotency: a retried POST with the same buyerReference returns the
    # order already on file instead of placing a duplicate — agents retry
    # on timeouts, and without this a flaky connection double-orders.
    if body.buyerReference:
        existing = session.exec(
            select(Order)
            .where(Order.agent_key_id == key.id)
            .where(Order.buyer_reference == body.buyerReference)
            .where(Order.status.in_([OrderStatus.created, OrderStatus.paid]))
        ).first()
        if existing is not None:
            gateway = get_gateway()
            return {
                "orderId": existing.id,
                "status": existing.status.value,
                "totalInr": existing.total_amount_inr,
                "currency": "INR",
                "payment": {
                    "razorpayOrderId": existing.razorpay_order_id,
                    "mode": "mock" if gateway.is_mock else "payment_link",
                },
                "mock": gateway.is_mock,
                "idempotent": True,
            }

    try:
        cart = price_cart(session, body.serviceId, body.addonIds)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    spent = _key_spend_24h(session, key.id)
    if spent + cart.total_inr > key.daily_limit_inr:
        reason = (
            f"agent key '{key.name}' has spent ₹{spent} in the last 24h; adding "
            f"₹{cart.total_inr} would exceed its daily limit of ₹{key.daily_limit_inr}"
        )
        log_action(
            session, "ai_buyer", "order_blocked",
            actor_ref=key.id, rationale=reason,
            amount_inr=cart.total_inr, bounds_check="blocked",
            detail={"rule": "key_daily_limit", "buyerReference": body.buyerReference},
        )
        return JSONResponse(status_code=400, content={"message": reason})

    try:
        order, rzp_order = create_order(
            session,
            cart,
            channel="agent_api",
            actor="ai_buyer",
            actor_ref=key.id,
            rationale=f"agent '{key.name}' placed an order"
            + (f" (buyer ref: {body.buyerReference})" if body.buyerReference else ""),
            user_id=AGENT_BUYER_USER_ID,
            agent_key_id=key.id,
            buyer_reference=body.buyerReference or None,
        )
    except BoundsExceeded as e:
        return JSONResponse(status_code=400, content={"message": e.result.reason})
    except PaymentGatewayError:
        return JSONResponse(
            status_code=502,
            content={"message": "The payment gateway is temporarily unavailable — please try again."},
        )
    except IntegrityError:
        # The read-then-write idempotency check above raced with a
        # concurrent duplicate request for the same buyerReference and both
        # passed it; the unique DB index caught the second insert. Recover
        # by returning the row that actually won, same shape as the
        # pre-check above.
        session.rollback()
        if body.buyerReference:
            existing = session.exec(
                select(Order)
                .where(Order.agent_key_id == key.id)
                .where(Order.buyer_reference == body.buyerReference)
            ).first()
            if existing is not None:
                gateway = get_gateway()
                return {
                    "orderId": existing.id,
                    "status": existing.status.value,
                    "totalInr": existing.total_amount_inr,
                    "currency": "INR",
                    "payment": {
                        "razorpayOrderId": existing.razorpay_order_id,
                        "mode": "mock" if gateway.is_mock else "payment_link",
                    },
                    "mock": gateway.is_mock,
                    "idempotent": True,
                }
        return JSONResponse(status_code=409, content={"message": "Duplicate order request."})

    gateway = get_gateway()
    payment: dict = {"razorpayOrderId": order.razorpay_order_id}
    if gateway.is_mock:
        payment["mode"] = "mock"
        payment["note"] = "Mock gateway: POST /api/agent/v1/orders/{orderId}/pay-mock to complete payment."
    else:
        link = gateway.create_payment_link(
            order.total_amount_inr,
            description=f"LexCart consultation — {cart.lawyer.name}",
            reference_id=order.id,
            notes={"orderId": order.id, "agentKey": key.name},
        )
        payment["mode"] = "payment_link"
        payment["url"] = link["short_url"]

    return {
        "orderId": order.id,
        "status": order.status.value,
        "totalInr": order.total_amount_inr,
        "currency": "INR",
        "lineItems": cart.line_items(),
        "payment": payment,
        "mock": gateway.is_mock,
    }


@router.post("/api/agent/v1/orders/{order_id}/pay-mock")
def agent_pay_mock(
    order_id: str,
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    """Close the loop headlessly on the mock gateway (403 with real keys):
    simulates the gateway callback, then runs the normal verify path."""
    gateway = get_gateway()
    if not gateway.is_mock:
        return JSONResponse(
            status_code=403,
            content={"message": "Real Razorpay keys configured — pay via the payment link instead."},
        )
    order = session.get(Order, order_id)
    if order is None or order.agent_key_id != key.id:
        return JSONResponse(status_code=404, content={"message": "Order not found"})
    if order.status != OrderStatus.created:
        return JSONResponse(status_code=400, content={"message": f"Order is already {order.status.value}"})

    payment_id, signature = gateway.mock_pay(order.razorpay_order_id)
    try:
        booking = confirm_payment(
            session, order, payment_id, signature, actor="ai_buyer", actor_ref=key.id
        )
    except OrderAlreadyProcessed as e:
        resolved = e.order
        if resolved.status == OrderStatus.paid and resolved.booking_id:
            return {
                "orderId": resolved.id,
                "status": "paid",
                "bookingId": resolved.booking_id,
                "razorpayPaymentId": resolved.razorpay_payment_id,
            }
        return JSONResponse(
            status_code=409, content={"message": f"Order is already {resolved.status.value}"}
        )
    except PaymentVerificationError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    return {
        "orderId": order.id,
        "status": "paid",
        "bookingId": booking.id,
        "razorpayPaymentId": payment_id,
    }


@router.post("/api/agent/v1/orders/{order_id}/cancel")
def agent_cancel_order(
    order_id: str,
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    """Cancel a paid order: full refund at the gateway, order marked
    refunded, any campaign spend/conversion it counted against rolled
    back. Only paid orders can be cancelled this way."""
    order = session.get(Order, order_id)
    if order is None or order.agent_key_id != key.id:
        return JSONResponse(status_code=404, content={"message": "Order not found"})
    try:
        order = refund_order(
            session, order, actor="ai_buyer", actor_ref=key.id,
            reason=f"agent '{key.name}' cancelled order {order.id} after payment; refunded in full",
        )
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    except PaymentGatewayError:
        return JSONResponse(
            status_code=502,
            content={"message": "The payment gateway is temporarily unavailable — please try again."},
        )
    return {
        "orderId": order.id,
        "status": order.status.value,
        "razorpayRefundId": order.razorpay_refund_id,
    }


@router.get("/api/agent/v1/orders/{order_id}")
def agent_order_status(
    order_id: str,
    key: AgentApiKey = Depends(get_agent_key),
    session: Session = Depends(get_session),
):
    order = session.get(Order, order_id)
    if order is None or order.agent_key_id != key.id:
        return JSONResponse(status_code=404, content={"message": "Order not found"})
    return order.to_dict()
