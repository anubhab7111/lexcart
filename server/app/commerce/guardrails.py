"""
Guardrails for agent-initiated money actions.

The bar for this track: every money action explainable, bounded, gated.
This module is the "bounded" part — hard caps checked server-side BEFORE
any Razorpay order or payment link exists. Results are returned as a
BoundsResult so callers can log the exact rule and numbers to the audit
trail rather than a bare boolean.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func as safunc
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Order, OrderStatus

AGENT_CHANNELS = ("concierge", "agent_api", "campaign")


@dataclass
class BoundsResult:
    ok: bool
    rule: str
    reason: str

    @property
    def audit_value(self) -> str:
        return "passed" if self.ok else "blocked"


def check_order_bounds(
    session: Session,
    total_inr: int,
    channel: str,
    *,
    user_id: str = "",
    agent_key_id: str = "",
) -> BoundsResult:
    """Bounds for one proposed order. Human web checkout is not capped
    (the human is the gate); agent-assisted channels are."""
    settings = get_settings()

    if channel not in AGENT_CHANNELS:
        return BoundsResult(True, "none", "human-initiated checkout; no agent cap applies")

    if total_inr > settings.agent_max_order_inr:
        return BoundsResult(
            False,
            "max_order",
            f"order total ₹{total_inr} exceeds the per-order agent cap "
            f"of ₹{settings.agent_max_order_inr}",
        )

    since = datetime.now(timezone.utc) - timedelta(days=1)
    since_open = datetime.now(timezone.utc) - timedelta(hours=1)
    # Paid orders in the last 24h, plus still-open ("created") orders from
    # the last hour — otherwise an agent could mint unlimited unpaid
    # Razorpay orders without ever tripping the cap.
    stmt = (
        select(safunc.coalesce(safunc.sum(Order.total_amount_inr), 0))
        .where(Order.channel.in_(AGENT_CHANNELS))
        .where(
            ((Order.status == OrderStatus.paid) & (Order.created_at >= since))
            | ((Order.status == OrderStatus.created) & (Order.created_at >= since_open))
        )
    )
    if agent_key_id:
        stmt = stmt.where(Order.agent_key_id == agent_key_id)
    elif user_id:
        stmt = stmt.where(Order.user_id == user_id)
    spent_today = int(session.exec(stmt).one())

    if spent_today + total_inr > settings.agent_daily_spend_cap_inr:
        return BoundsResult(
            False,
            "daily_cap",
            f"₹{spent_today} already spent or pending via agents in the last 24h "
            f"(open orders counted for 1h); adding ₹{total_inr} would exceed the "
            f"daily cap of ₹{settings.agent_daily_spend_cap_inr}",
        )

    return BoundsResult(
        True,
        "max_order+daily_cap",
        f"₹{total_inr} within per-order cap ₹{settings.agent_max_order_inr}; "
        f"24h agent spend ₹{spent_today}/{settings.agent_daily_spend_cap_inr}",
    )


def check_campaign_bounds(discount_pct: int, budget_inr: int) -> BoundsResult:
    settings = get_settings()
    if discount_pct < 0 or discount_pct > settings.campaign_max_discount_pct:
        return BoundsResult(
            False,
            "max_discount",
            f"discount {discount_pct}% outside the allowed 0–"
            f"{settings.campaign_max_discount_pct}% range",
        )
    if budget_inr <= 0 or budget_inr > settings.campaign_max_budget_inr:
        return BoundsResult(
            False,
            "max_budget",
            f"budget ₹{budget_inr} outside the allowed range "
            f"(max ₹{settings.campaign_max_budget_inr})",
        )
    return BoundsResult(
        True,
        "max_discount+max_budget",
        f"discount {discount_pct}% ≤ {settings.campaign_max_discount_pct}%, "
        f"budget ₹{budget_inr} ≤ ₹{settings.campaign_max_budget_inr}",
    )
