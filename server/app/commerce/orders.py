"""
Order pricing + lifecycle shared by every checkout surface (web Payment
page, concierge chat, AI-buyer API, campaign links).

Invariants enforced here, not in routers:
- amounts are always recomputed server-side from the DB (client/agent
  totals are never trusted);
- bounds are checked before a Razorpay order exists;
- every order creation / payment / failure writes an audit row.
"""

import math
import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.guardrails import AGENT_CHANNELS, BoundsResult, check_order_bounds
from app.db.models import (
    Booking,
    BookingStatus,
    Campaign,
    CampaignStatus,
    Lawyer,
    Order,
    OrderStatus,
    ServiceAddon,
)
from app.services.razorpay_gateway import PaymentGatewayError, get_gateway

PLATFORM_FEE_RATE = 0.05


class CartError(ValueError):
    """Invalid cart contents (unknown lawyer/addon, inactive campaign...)."""


class BoundsExceeded(Exception):
    def __init__(self, result: BoundsResult):
        super().__init__(result.reason)
        self.result = result


@dataclass
class PricedCart:
    lawyer: Lawyer
    addons: List[ServiceAddon] = field(default_factory=list)
    campaign: Optional[Campaign] = None
    base_inr: int = 0
    addon_inr: int = 0
    fee_inr: int = 0
    discount_inr: int = 0
    total_inr: int = 0

    def line_items(self) -> list:
        items = [{"label": f"Consultation — {self.lawyer.name}", "amountInr": self.base_inr}]
        items += [{"label": a.name, "amountInr": a.price_inr} for a in self.addons]
        items.append({"label": "Platform fee (5%)", "amountInr": self.fee_inr})
        if self.discount_inr:
            items.append(
                {
                    "label": f"Campaign discount ({self.campaign.discount_pct}%)",
                    "amountInr": -self.discount_inr,
                }
            )
        return items


def price_cart(
    session: Session,
    lawyer_id: str,
    addon_ids: Optional[List[str]] = None,
    campaign_id: Optional[str] = None,
) -> PricedCart:
    lawyer = session.get(Lawyer, lawyer_id)
    if lawyer is None:
        raise CartError(f"Lawyer {lawyer_id!r} not found")

    addons: List[ServiceAddon] = []
    for aid in dict.fromkeys(addon_ids or []):
        addon = session.get(ServiceAddon, aid)
        if addon is None or not addon.active:
            raise CartError(f"Addon {aid!r} not found or inactive")
        addons.append(addon)

    campaign = None
    if campaign_id:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None or campaign.status != CampaignStatus.active:
            raise CartError(f"Campaign {campaign_id!r} not found or not active")

    cart = PricedCart(lawyer=lawyer, addons=addons, campaign=campaign)
    cart.base_inr = lawyer.hourly_rate
    cart.addon_inr = sum(a.price_inr for a in addons)
    # Mirror the legacy client rule: 5% fee on the consultation, JS-style
    # round-half-up.
    cart.fee_inr = math.floor(cart.base_inr * PLATFORM_FEE_RATE + 0.5)
    if campaign:
        raw_discount = math.floor((cart.base_inr + cart.addon_inr) * campaign.discount_pct / 100)
        remaining_budget = max(campaign.budget_inr - campaign.spent_inr, 0)
        cart.discount_inr = min(raw_discount, remaining_budget)
    cart.total_inr = cart.base_inr + cart.addon_inr + cart.fee_inr - cart.discount_inr
    return cart


def create_order(
    session: Session,
    cart: PricedCart,
    channel: str,
    actor: str,
    actor_ref: str,
    rationale: str,
    *,
    user_id: Optional[str] = None,
    agent_key_id: Optional[str] = None,
    buyer_reference: Optional[str] = None,
    gate_status: str = "not_required",
) -> Tuple[Order, dict]:
    """Bounds-check the priced cart, create the Razorpay order, persist our
    Order row, and audit — or audit the refusal and raise BoundsExceeded."""
    if channel in AGENT_CHANNELS:
        # Serialize concurrent bounds-check + insert for the same actor
        # scope. Without this, two concurrent agent orders for the same
        # user/key can each run check_order_bounds's SUM query before
        # either has inserted its own order, both see the same "spent so
        # far" total, and both pass the daily cap even though together
        # they exceed it. pg_advisory_xact_lock is transaction-scoped: it
        # releases automatically on this function's session.commit() (or
        # on rollback if bounds fail), so no explicit unlock is needed,
        # and it only blocks other callers sharing the same scope key —
        # unrelated actors aren't serialized against each other.
        lock_scope = agent_key_id or user_id or channel
        lock_key = zlib.crc32(f"order_bounds:{lock_scope}".encode()) & 0x7FFFFFFF
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    bounds = check_order_bounds(
        session,
        cart.total_inr,
        channel,
        user_id=user_id or "",
        agent_key_id=agent_key_id or "",
    )
    if not bounds.ok:
        log_action(
            session,
            actor,
            "order_blocked",
            actor_ref=actor_ref,
            user_id=user_id,
            rationale=bounds.reason,
            amount_inr=cart.total_inr,
            bounds_check="blocked",
            gate_status=gate_status,
            detail={"rule": bounds.rule, "lawyerId": cart.lawyer.id},
        )
        raise BoundsExceeded(bounds)

    gateway = get_gateway()
    order = Order(
        user_id=user_id,
        lawyer_id=cart.lawyer.id,
        addon_ids=[a.id for a in cart.addons],
        base_amount_inr=cart.base_inr,
        addon_amount_inr=cart.addon_inr,
        fee_amount_inr=cart.fee_inr,
        discount_amount_inr=cart.discount_inr,
        total_amount_inr=cart.total_inr,
        razorpay_order_id="pending",
        channel=channel,
        campaign_id=cart.campaign.id if cart.campaign else None,
        agent_key_id=agent_key_id,
        buyer_reference=buyer_reference,
    )
    try:
        rzp_order = gateway.create_order(
            cart.total_inr,
            receipt=order.id,
            notes={
                "channel": channel,
                "lawyer": cart.lawyer.name,
                "userId": user_id or "",
            },
        )
    except PaymentGatewayError as e:
        # Nothing was persisted yet (order.id only exists in memory as the
        # Razorpay receipt), but a money-action failure still needs an
        # audit row -- "every action bounded, gated, audited" includes the
        # ones that fail, not just the ones that succeed.
        log_action(
            session,
            actor,
            "order_failed",
            actor_ref=actor_ref,
            user_id=user_id,
            rationale=f"Razorpay order creation failed: {e}",
            amount_inr=cart.total_inr,
            bounds_check=bounds.audit_value,
            gate_status=gate_status,
            detail={"lawyerId": cart.lawyer.id, "error": str(e)},
        )
        raise
    order.razorpay_order_id = rzp_order["id"]
    session.add(order)
    session.commit()
    session.refresh(order)

    log_action(
        session,
        actor,
        "order_created",
        actor_ref=actor_ref,
        user_id=user_id,
        rationale=rationale,
        amount_inr=cart.total_inr,
        bounds_check=bounds.audit_value,
        gate_status=gate_status,
        order_id=order.id,
        detail={
            "razorpayOrderId": order.razorpay_order_id,
            "lineItems": cart.line_items(),
            "boundsRule": bounds.rule,
            "boundsReason": bounds.reason,
            "mock": rzp_order.get("mock", False),
        },
    )
    return order, rzp_order


class PaymentVerificationError(Exception):
    pass


class OrderAlreadyProcessed(Exception):
    """Raised when confirm_payment finds, under a row lock, that a
    concurrent caller already resolved this order. Three surfaces can all
    reach confirm_payment for the same order -- the client-side /verify
    callback, an agent's pay-mock call, and the Razorpay webhook safety
    net -- and each has its own read-then-act status check with a TOCTOU
    gap before it ever calls this function. This is not a user error:
    callers should look up the order's current state and reply with that
    outcome instead of treating it as a failure."""

    def __init__(self, order: Order):
        super().__init__(f"Order {order.id} is already {order.status.value}")
        self.order = order


def confirm_payment(
    session: Session,
    order: Order,
    payment_id: str,
    signature: str,
    actor: str,
    actor_ref: str,
    *,
    pre_verified: bool = False,
) -> Booking:
    """Verify the gateway signature and, on success, mark the order paid and
    create the confirmed booking. On a bad signature the order is marked
    failed, the failure audited, and PaymentVerificationError raised —
    the graceful-failure path, not an unhandled one.

    `pre_verified=True` skips the per-payment HMAC check: used by the
    webhook handler, which has already authenticated the whole event via
    the separate webhook signature (see app.routers.webhooks) and is
    reconciling a payment the client-side callback may never have reached
    the server for.
    """
    # Atomically claim the order row under a lock before doing anything
    # else. Every caller of this function has already read order.status in
    # its own transaction and decided it looked like "created" -- but that
    # read is not the gate, because three different surfaces can race to
    # get here for the same order (see OrderAlreadyProcessed above).
    #
    # session.refresh(..., with_for_update=True) rather than a plain
    # `select(...).with_for_update()`: the caller's `order` object is
    # already attached to this session's identity map (every real call
    # site loads it on the same session it hands to this function), and a
    # plain SELECT re-run on a session that already has that row loaded
    # blocks correctly at the DB level but then hands back the *cached*
    # Python object without overwriting its already-loaded attributes --
    # so a concurrent caller can unblock, still see the stale
    # status="created" it read before waiting, and double-process anyway.
    # refresh() is the one Session API that forces an unconditional
    # attribute overwrite from the freshly (lock-)fetched row.
    session.refresh(order, with_for_update=True)
    if order.status != OrderStatus.created:
        raise OrderAlreadyProcessed(order)

    gateway = get_gateway()
    if not pre_verified and not gateway.verify_payment_signature(
        order.razorpay_order_id, payment_id, signature
    ):
        order.status = OrderStatus.failed
        order.failure_reason = "signature verification failed"
        session.add(order)
        log_action(
            session,
            actor,
            "payment_failed",
            actor_ref=actor_ref,
            user_id=order.user_id,
            rationale="gateway signature did not verify; no booking created, order marked failed",
            amount_inr=order.total_amount_inr,
            order_id=order.id,
            detail={"razorpayOrderId": order.razorpay_order_id, "paymentId": payment_id},
        )
        raise PaymentVerificationError(
            "Payment could not be verified. Nothing was booked — please retry."
        )

    order.status = OrderStatus.paid
    order.razorpay_payment_id = payment_id
    session.add(order)

    try:
        booking = Booking(
            user_id=order.user_id,
            lawyer_id=order.lawyer_id,
            amount=order.total_amount_inr,
            status=BookingStatus.confirmed,
            transaction_id=payment_id,
        )
        session.add(booking)
        session.flush()
    except Exception as e:
        # Money was captured at the gateway but the booking couldn't be
        # created — refund immediately rather than keep money for a
        # service that was never delivered, and say so plainly.
        session.rollback()
        order = session.get(Order, order.id)
        try:
            refund = gateway.refund_payment(payment_id, order.total_amount_inr)
        except PaymentGatewayError as refund_err:
            # The worst case: money was captured, the booking failed, and
            # the automatic refund also failed at the gateway. Leave the
            # order in a distinctly-flagged, unmistakably-broken state
            # (not silently "created", not incorrectly "refunded") and
            # audit both failures so this is findable, not lost to a
            # stack trace.
            order.failure_reason = (
                f"booking creation failed post-capture ({e}) AND the automatic "
                f"refund also failed ({refund_err}) — payment was captured but "
                "neither delivered nor refunded; needs manual reconciliation"
            )
            session.add(order)
            log_action(
                session, actor, "refund_failed",
                actor_ref=actor_ref, user_id=order.user_id,
                rationale=order.failure_reason,
                amount_inr=order.total_amount_inr, order_id=order.id,
                detail={
                    "razorpayPaymentId": payment_id,
                    "bookingError": str(e),
                    "refundError": str(refund_err),
                },
            )
            raise PaymentVerificationError(
                "Payment was captured but we couldn't complete the booking or the "
                "automatic refund. This has been flagged for manual review — please "
                "contact support with your payment id."
            ) from refund_err
        order.status = OrderStatus.refunded
        order.razorpay_refund_id = refund["id"]
        order.failure_reason = f"booking creation failed post-capture: {e}"
        session.add(order)
        log_action(
            session, actor, "refund_issued",
            actor_ref=actor_ref, user_id=order.user_id,
            rationale="payment captured but the booking could not be created; "
            "refunded automatically so no money was kept for an undelivered service",
            amount_inr=order.total_amount_inr, order_id=order.id,
            detail={"razorpayPaymentId": payment_id, "razorpayRefundId": refund["id"]},
        )
        raise PaymentVerificationError(
            "Payment was captured but booking failed — it has been refunded automatically."
        ) from e

    order.booking_id = booking.id
    session.add(order)

    if order.campaign_id:
        campaign = session.get(Campaign, order.campaign_id)
        if campaign:
            campaign.conversions += 1
            campaign.spent_inr += order.discount_amount_inr
            if campaign.spent_inr >= campaign.budget_inr:
                campaign.status = CampaignStatus.completed
            session.add(campaign)

    log_action(
        session,
        actor,
        "payment_confirmed",
        actor_ref=actor_ref,
        user_id=order.user_id,
        rationale="gateway signature verified; booking confirmed"
        if not pre_verified
        else "gateway webhook confirmed capture; booking confirmed (reconciled server-side)",
        amount_inr=order.total_amount_inr,
        order_id=order.id,
        gate_status="approved" if order.channel in ("concierge", "agent_api") else "not_required",
        detail={
            "razorpayPaymentId": payment_id,
            "bookingId": booking.id,
            "campaignId": order.campaign_id,
        },
        commit=False,
    )
    session.commit()
    session.refresh(booking)
    return booking


def refund_order(
    session: Session,
    order: Order,
    actor: str,
    actor_ref: str,
    reason: str,
) -> Order:
    """Cancel a paid order: refund the full amount at the gateway, mark the
    order refunded, and roll back any campaign spend/conversion it was
    counted against. Does not touch the booking record itself — cancelling
    the underlying consultation is a separate, human decision."""
    if order.status != OrderStatus.paid:
        raise CartError(f"Order is {order.status.value}, not paid — nothing to refund")

    gateway = get_gateway()
    try:
        refund = gateway.refund_payment(order.razorpay_payment_id, order.total_amount_inr)
    except PaymentGatewayError as e:
        log_action(
            session, actor, "refund_failed",
            actor_ref=actor_ref, user_id=order.user_id,
            rationale=f"cancellation requested but the gateway refund failed: {e}",
            amount_inr=order.total_amount_inr, order_id=order.id,
            detail={"razorpayPaymentId": order.razorpay_payment_id, "error": str(e)},
        )
        raise
    order.status = OrderStatus.refunded
    order.razorpay_refund_id = refund["id"]
    session.add(order)

    if order.campaign_id:
        campaign = session.get(Campaign, order.campaign_id)
        if campaign:
            campaign.conversions = max(campaign.conversions - 1, 0)
            campaign.spent_inr = max(campaign.spent_inr - order.discount_amount_inr, 0)
            if campaign.status == CampaignStatus.completed and campaign.spent_inr < campaign.budget_inr:
                campaign.status = CampaignStatus.active
            session.add(campaign)

    log_action(
        session, actor, "refund_issued",
        actor_ref=actor_ref, user_id=order.user_id,
        rationale=reason, amount_inr=order.total_amount_inr, order_id=order.id,
        detail={"razorpayPaymentId": order.razorpay_payment_id, "razorpayRefundId": refund["id"]},
        commit=False,
    )
    session.commit()
    session.refresh(order)
    return order


def confirm_campaign_link_payment(
    session: Session,
    campaign: Campaign,
    razorpay_payment_id: str,
    buyer_user_id: str,
    actor: str,
    actor_ref: str,
) -> Order:
    """Reconcile a paid campaign payment link (webhook-only path).

    Unlike every other checkout surface, a campaign link is paid by
    whoever clicked the shared link outside any in-app cart, so there is
    no pre-existing Order row to update — this webhook event is the only
    place the order and booking get created at all. Idempotent on
    razorpay_payment_id so a duplicate webhook delivery is a no-op.
    """
    existing = session.exec(
        select(Order).where(Order.razorpay_payment_id == razorpay_payment_id)
    ).first()
    if existing:
        return existing

    lawyer = session.get(Lawyer, campaign.lawyer_id)
    if lawyer is None:
        raise CartError("Campaign lawyer missing")
    discount_inr = math.floor(lawyer.hourly_rate * campaign.discount_pct / 100)
    total_inr = lawyer.hourly_rate - discount_inr

    order = Order(
        user_id=buyer_user_id,
        lawyer_id=lawyer.id,
        addon_ids=[],
        base_amount_inr=lawyer.hourly_rate,
        discount_amount_inr=discount_inr,
        total_amount_inr=total_inr,
        razorpay_order_id=f"link_{campaign.payment_link_id}_{razorpay_payment_id}",
        razorpay_payment_id=razorpay_payment_id,
        status=OrderStatus.paid,
        channel="campaign",
        campaign_id=campaign.id,
    )
    session.add(order)
    try:
        session.flush()
    except IntegrityError:
        # The read-then-write dedup check above (`existing = ...`) has a
        # race window: two duplicate webhook deliveries for the same
        # payment can both miss it and both reach this insert. The unique
        # index on razorpay_payment_id turns the loser's insert into this
        # IntegrityError instead of a second order+booking — recover by
        # returning whichever row actually won.
        session.rollback()
        existing = session.exec(
            select(Order).where(Order.razorpay_payment_id == razorpay_payment_id)
        ).first()
        if existing:
            return existing
        raise

    booking = Booking(
        user_id=buyer_user_id,
        lawyer_id=lawyer.id,
        amount=total_inr,
        status=BookingStatus.confirmed,
        transaction_id=razorpay_payment_id,
    )
    session.add(booking)
    session.flush()
    order.booking_id = booking.id
    session.add(order)

    campaign.conversions += 1
    campaign.spent_inr += discount_inr
    if campaign.spent_inr >= campaign.budget_inr:
        campaign.status = CampaignStatus.completed
    session.add(campaign)

    log_action(
        session, actor, "payment_confirmed",
        actor_ref=actor_ref, user_id=buyer_user_id,
        rationale=f"Razorpay webhook confirmed payment_link.paid for campaign "
        f"'{campaign.name}'; booking created",
        amount_inr=total_inr, order_id=order.id, gate_status="not_required",
        detail={
            "razorpayPaymentId": razorpay_payment_id,
            "campaignId": campaign.id,
            "bookingId": booking.id,
        },
        commit=False,
    )
    session.commit()
    session.refresh(order)
    return order


def get_order_for_update(
    session: Session, order_id: str, *, user_id: Optional[str] = None
) -> Order:
    order = session.exec(select(Order).where(Order.id == order_id)).first()
    if order is None:
        raise CartError("Order not found")
    if user_id is not None and order.user_id != user_id:
        raise CartError("Order not found")
    if order.status != OrderStatus.created:
        raise CartError(f"Order is already {order.status.value}")
    return order
