"""
Merchant growth metrics: the numbers that make "agentic commerce grows
revenue" a claim you can point at instead of just assert. Computed
straight from the orders/agent_actions tables — no separate analytics
pipeline, so the figures can never drift from the audit trail itself.
"""

from fastapi import APIRouter, Depends
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
    paid = session.exec(select(Order).where(Order.status == OrderStatus.paid)).all()

    revenue_by_channel: dict = {}
    for o in paid:
        revenue_by_channel[o.channel] = revenue_by_channel.get(o.channel, 0) + o.total_amount_inr
    total_revenue = sum(revenue_by_channel.values())
    agentic_revenue = sum(v for k, v in revenue_by_channel.items() if k != "web")

    with_addons = sum(1 for o in paid if o.addon_amount_inr > 0)
    upsell_attach_rate = round(100 * with_addons / len(paid), 1) if paid else 0.0

    campaign_orders = [o for o in paid if o.campaign_id]
    campaign_attributed_revenue = sum(o.total_amount_inr for o in campaign_orders)
    campaign_discount_spend = sum(o.discount_amount_inr for o in campaign_orders)
    campaign_roi = (
        round(campaign_attributed_revenue / campaign_discount_spend, 2)
        if campaign_discount_spend
        else None
    )

    refusals = session.exec(
        select(AgentAction).where(AgentAction.action.in_(_REFUSAL_ACTIONS))
    ).all()

    return {
        "totalRevenueInr": total_revenue,
        "revenueByChannel": revenue_by_channel,
        "agenticRevenueInr": agentic_revenue,
        "agenticSharePct": round(100 * agentic_revenue / total_revenue, 1) if total_revenue else 0.0,
        "paidOrderCount": len(paid),
        "upsellAttachRatePct": upsell_attach_rate,
        "campaignAttributedRevenueInr": campaign_attributed_revenue,
        "campaignDiscountSpendInr": campaign_discount_spend,
        "campaignRoi": campaign_roi,
        "guardrailRefusalCount": len(refusals),
    }
