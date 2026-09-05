"""
Merchant growth metrics: the numbers that make "agentic commerce grows
revenue" a claim you can point at instead of just assert. Computed
straight from the orders/agent_actions tables — no separate analytics
pipeline, so the figures can never drift from the audit trail itself.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func as safunc
from sqlmodel import Session, select

from app.db.engine import get_session
from app.db.models import AgentAction, Order, OrderStatus, User
from app.deps.auth import require_merchant

router = APIRouter(prefix="/api/merchant", tags=["merchant"])

_REFUSAL_ACTIONS = ("order_blocked", "checkout_blocked", "campaign_blocked")


@router.get("/stats")
def merchant_stats(
    current_user: User = Depends(require_merchant),
    session: Session = Depends(get_session),
):
    # Aggregated in SQL rather than loading every paid order into Python —
    # correct at demo scale either way, but this doesn't grow linearly with
    # order volume.
    paid_count, total_revenue = session.exec(
        select(
            safunc.count(Order.id),
            safunc.coalesce(safunc.sum(Order.total_amount_inr), 0),
        ).where(Order.status == OrderStatus.paid)
    ).one()
    total_revenue = int(total_revenue)

    revenue_by_channel = {
        channel: int(revenue)
        for channel, revenue in session.exec(
            select(Order.channel, safunc.sum(Order.total_amount_inr))
            .where(Order.status == OrderStatus.paid)
            .group_by(Order.channel)
        ).all()
    }
    agentic_revenue = sum(v for k, v in revenue_by_channel.items() if k != "web")

    with_addons = session.exec(
        select(safunc.count(Order.id))
        .where(Order.status == OrderStatus.paid)
        .where(Order.addon_amount_inr > 0)
    ).one()
    upsell_attach_rate = round(100 * with_addons / paid_count, 1) if paid_count else 0.0

    campaign_attributed_revenue, campaign_discount_spend = session.exec(
        select(
            safunc.coalesce(safunc.sum(Order.total_amount_inr), 0),
            safunc.coalesce(safunc.sum(Order.discount_amount_inr), 0),
        )
        .where(Order.status == OrderStatus.paid)
        .where(Order.campaign_id.is_not(None))
    ).one()
    campaign_attributed_revenue = int(campaign_attributed_revenue)
    campaign_discount_spend = int(campaign_discount_spend)
    campaign_roi = (
        round(campaign_attributed_revenue / campaign_discount_spend, 2)
        if campaign_discount_spend
        else None
    )

    refusal_count = session.exec(
        select(safunc.count(AgentAction.id)).where(AgentAction.action.in_(_REFUSAL_ACTIONS))
    ).one()

    return {
        "totalRevenueInr": total_revenue,
        "revenueByChannel": revenue_by_channel,
        "agenticRevenueInr": agentic_revenue,
        "agenticSharePct": round(100 * agentic_revenue / total_revenue, 1) if total_revenue else 0.0,
        "paidOrderCount": paid_count,
        "upsellAttachRatePct": upsell_attach_rate,
        "campaignAttributedRevenueInr": campaign_attributed_revenue,
        "campaignDiscountSpendInr": campaign_discount_spend,
        "campaignRoi": campaign_roi,
        "guardrailRefusalCount": refusal_count,
    }
