"""
Booking/payment endpoints on Razorpay test mode.

Flow (web checkout):
  POST /create-order  → server prices the cart, creates the Razorpay order
  <checkout.js or mock-pay collects payment>
  POST /verify        → HMAC signature check → confirmed booking
  POST /failure       → audit a checkout.js failure so nothing is silent

Amounts are always recomputed server-side; the client never dictates a
price. Every step lands in the agent_actions audit trail.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.orders import (
    BoundsExceeded,
    CartError,
    OrderAlreadyProcessed,
    PaymentVerificationError,
    confirm_payment,
    create_order,
    get_order_for_update,
    price_cart,
)
from app.db.engine import get_session
from app.db.models import Booking, BookingStatus, Order, OrderStatus, User
from app.deps.auth import get_current_user
from app.deps.errors import MessageHTTPException
from app.services.razorpay_gateway import PaymentGatewayError, get_gateway

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


@router.get("/config")
def payment_config():
    """What the browser checkout needs: the public key id, and whether the
    backend is on the mock gateway (no keys in .env) or real test mode."""
    gateway = get_gateway()
    return {"keyId": gateway.public_key_id, "mock": gateway.is_mock}


class CreateOrderRequest(BaseModel):
    lawyerId: str
    addonIds: List[str] = []
    campaignId: Optional[str] = None


@router.post("/create-order")
def create_checkout_order(
    body: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        cart = price_cart(session, body.lawyerId, body.addonIds, body.campaignId)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    try:
        order, rzp_order = create_order(
            session,
            cart,
            channel="web",
            actor="web_checkout",
            actor_ref=current_user.id,
            rationale="user-initiated checkout from the Payment page",
            user_id=current_user.id,
        )
    except BoundsExceeded as e:
        return JSONResponse(status_code=400, content={"message": e.result.reason})
    except PaymentGatewayError:
        return JSONResponse(
            status_code=502,
            content={"message": "The payment gateway is temporarily unavailable — please try again."},
        )

    gateway = get_gateway()
    return {
        "orderId": order.id,
        "razorpayOrderId": order.razorpay_order_id,
        "amountInr": order.total_amount_inr,
        "amountPaise": rzp_order["amount"],
        "currency": "INR",
        "keyId": gateway.public_key_id,
        "mock": gateway.is_mock,
        "lineItems": cart.line_items(),
    }


class VerifyRequest(BaseModel):
    orderId: str
    razorpayPaymentId: str
    razorpaySignature: str


@router.post("/verify")
def verify_checkout(
    body: VerifyRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    # The Razorpay webhook can beat checkout.js's own success callback to
    # this order (both carry a genuinely successful payment). If that
    # already happened, the payment succeeded and the booking already
    # exists -- replay that same success instead of erroring on a race
    # the user did nothing wrong to trigger.
    existing = session.get(Order, body.orderId)
    if existing is not None and existing.user_id == current_user.id and existing.status == OrderStatus.paid:
        booking = session.get(Booking, existing.booking_id) if existing.booking_id else None
        if booking is not None:
            return {"status": "success", "transactionId": booking.transaction_id, "bookingId": booking.id}

    try:
        order = get_order_for_update(session, body.orderId, user_id=current_user.id)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    try:
        booking = confirm_payment(
            session,
            order,
            body.razorpayPaymentId,
            body.razorpaySignature,
            actor="web_checkout",
            actor_ref=current_user.id,
        )
    except OrderAlreadyProcessed as e:
        # The webhook (or a duplicate client retry) already resolved this
        # order while we were waiting on confirm_payment's row lock. If it
        # ended up paid, that's a success from this user's perspective --
        # replay it instead of erroring on a race they didn't cause.
        resolved = e.order
        if resolved.status == OrderStatus.paid and resolved.booking_id:
            existing_booking = session.get(Booking, resolved.booking_id)
            if existing_booking is not None:
                return {
                    "status": "success",
                    "transactionId": existing_booking.transaction_id,
                    "bookingId": existing_booking.id,
                }
        return JSONResponse(
            status_code=409, content={"message": f"This order is already {resolved.status.value}."}
        )
    except PaymentVerificationError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

    return {
        "status": "success",
        "transactionId": booking.transaction_id,
        "bookingId": booking.id,
    }


class MockPayRequest(BaseModel):
    orderId: str
    fail: bool = False


@router.post("/mock-pay")
def mock_pay(
    body: MockPayRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Mock-gateway stand-in for checkout.js: returns the payment id and
    signature the gateway callback would carry. 403s when real keys are
    configured. `fail=true` produces a bad signature to demo the graceful
    failure path."""
    gateway = get_gateway()
    if not gateway.is_mock:
        return JSONResponse(
            status_code=403,
            content={"message": "Mock payments are disabled: real Razorpay keys are configured."},
        )
    try:
        order = get_order_for_update(session, body.orderId, user_id=current_user.id)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    payment_id, signature = gateway.mock_pay(order.razorpay_order_id, fail=body.fail)
    return {"razorpayPaymentId": payment_id, "razorpaySignature": signature}


class FailureRequest(BaseModel):
    orderId: str
    reason: str = "payment failed or was dismissed in checkout"


@router.post("/failure")
def report_failure(
    body: FailureRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Client-reported checkout failure (user closed the modal, card
    declined). Marks the order failed and audits it — no silent losses."""
    try:
        order = get_order_for_update(session, body.orderId, user_id=current_user.id)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    order.status = OrderStatus.failed
    order.failure_reason = body.reason[:500]
    session.add(order)
    log_action(
        session,
        "web_checkout",
        "payment_failed",
        actor_ref=current_user.id,
        user_id=current_user.id,
        rationale=f"client reported checkout failure: {body.reason[:200]}",
        amount_inr=order.total_amount_inr,
        order_id=order.id,
    )
    return {"status": "recorded"}


@router.get("/user-bookings/{user_id}")
def user_bookings(
    user_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Confirmed appointments for a user, newest first. Scoped to the caller —
    the path id must match the authenticated user (no cross-user reads)."""
    if user_id != current_user.id:
        raise MessageHTTPException(status_code=404, detail="Not found")
    try:
        bookings = session.exec(
            select(Booking)
            .where(Booking.user_id == user_id)
            .where(Booking.status == BookingStatus.confirmed)
            .order_by(Booking.created_at.desc())
        ).all()
        return [booking.to_dict() for booking in bookings]
    except Exception as e:
        print(f"Fetch Bookings Error: {e}")
        return JSONResponse(status_code=500, content={"message": "Fetch failed"})


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    order = session.get(Order, order_id)
    if order is None or order.user_id != current_user.id:
        raise MessageHTTPException(status_code=404, detail="Not found")
    return order.to_dict()
