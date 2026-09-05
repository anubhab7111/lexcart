"""
Campaign orchestrator: the agent drafts promo campaigns; the merchant is
the gate. A draft never discounts anything — only an explicitly approved
campaign gets a Razorpay payment link and becomes usable at checkout
(orders carry campaignId; confirm_payment tracks conversions and spend
against the budget, auto-completing the campaign when exhausted).

Demo note: every signed-in user acts as the merchant here; a real
deployment would gate these routes on an admin/merchant role.
"""

import json
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.guardrails import check_campaign_bounds
from app.db.engine import get_session
from app.db.models import AgentAction, Campaign, CampaignStatus, Lawyer, User
from app.deps.auth import get_current_user, require_merchant
from app.services.razorpay_gateway import PaymentGatewayError, get_gateway

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

_DRAFT_PROMPT = """You are a growth marketer for LexCart, an Indian legal-services marketplace.
Draft one promo campaign. Objective: {objective}
Service: consultation with {lawyer_name} ({specialty}, ₹{rate}).
Discount: {discount}% off. Reply with ONLY JSON:
{{"name": "<catchy campaign name, max 6 words>", "target_segment": "<who this targets, one phrase>", "message": "<SMS/WhatsApp promo message, max 40 words, mention the discount>"}}
JSON:"""


def _fallback_draft(objective: str, lawyer: Lawyer, discount_pct: int) -> dict:
    return {
        "name": f"{lawyer.specialty} {discount_pct}% Off Week",
        "target_segment": f"visitors who browsed {lawyer.specialty} but did not book",
        "message": (
            f"Get {discount_pct}% off a consultation with {lawyer.name} "
            f"({lawyer.specialty}, rated {lawyer.rating}★) on LexCart this week. "
            f"Objective: {objective[:60]}"
        ),
    }


async def _draft_with_llm(objective: str, lawyer: Lawyer, discount_pct: int) -> dict:
    try:
        from app.chatbot import get_fast_llm, invoke_llm_safely, strip_reasoning_tags

        raw = await invoke_llm_safely(
            get_fast_llm(),
            _DRAFT_PROMPT.format(
                objective=objective[:200],
                lawyer_name=lawyer.name,
                specialty=lawyer.specialty,
                rate=lawyer.hourly_rate,
                discount=discount_pct,
            ),
            stream=False,
        )
        match = re.search(r"\{.*\}", strip_reasoning_tags(raw), re.DOTALL)
        if match:
            draft = json.loads(match.group(0))
            if all(isinstance(draft.get(k), str) and draft[k] for k in ("name", "target_segment", "message")):
                return draft
    except Exception as e:
        print(f"[Campaigns] LLM draft failed, using template: {e}")
    return _fallback_draft(objective, lawyer, discount_pct)


class DraftRequest(BaseModel):
    objective: str
    lawyerId: str
    discountPct: int = 15
    budgetInr: int = 10000


@router.post("/draft")
async def draft_campaign(
    body: DraftRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    lawyer = session.get(Lawyer, body.lawyerId)
    if lawyer is None:
        return JSONResponse(status_code=400, content={"message": "Lawyer not found"})

    bounds = check_campaign_bounds(body.discountPct, body.budgetInr)
    if not bounds.ok:
        log_action(
            session, "campaign_agent", "campaign_blocked",
            actor_ref=current_user.id, user_id=current_user.id,
            rationale=bounds.reason, bounds_check="blocked",
            detail={"rule": bounds.rule, "objective": body.objective[:200]},
        )
        return JSONResponse(status_code=400, content={"message": bounds.reason})

    draft = await _draft_with_llm(body.objective, lawyer, body.discountPct)
    campaign = Campaign(
        name=draft["name"][:80],
        objective=body.objective[:500],
        target_segment=draft["target_segment"][:200],
        lawyer_id=lawyer.id,
        discount_pct=body.discountPct,
        budget_inr=body.budgetInr,
        message=draft["message"][:500],
        status=CampaignStatus.draft,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    log_action(
        session, "campaign_agent", "campaign_drafted",
        actor_ref=current_user.id, user_id=current_user.id,
        rationale=f"drafted '{campaign.name}' targeting {campaign.target_segment}; "
        f"{bounds.reason}. No discount is live until the merchant approves.",
        amount_inr=body.budgetInr,
        bounds_check="passed", gate_status="pending",
        detail={"campaignId": campaign.id, "discountPct": body.discountPct},
    )
    return campaign.to_dict()


@router.post("/{campaign_id}/approve")
def approve_campaign(
    campaign_id: str,
    current_user: User = Depends(require_merchant),
    session: Session = Depends(get_session),
):
    """The merchant gate: activates the campaign and mints its Razorpay
    payment link (the shareable outreach artifact)."""
    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.status != CampaignStatus.draft:
        return JSONResponse(status_code=400, content={"message": "Campaign not found or not a draft"})
    lawyer = session.get(Lawyer, campaign.lawyer_id) if campaign.lawyer_id else None
    if lawyer is None:
        return JSONResponse(status_code=400, content={"message": "Campaign lawyer missing"})

    discounted = lawyer.hourly_rate - (lawyer.hourly_rate * campaign.discount_pct) // 100
    try:
        link = get_gateway().create_payment_link(
            discounted,
            description=f"{campaign.name} — consultation with {lawyer.name} "
            f"({campaign.discount_pct}% off)",
            reference_id=campaign.id,
            notes={"campaignId": campaign.id},
        )
    except PaymentGatewayError as e:
        return JSONResponse(status_code=502, content={"message": str(e)})

    from datetime import datetime, timezone

    campaign.status = CampaignStatus.active
    campaign.payment_link_id = link["id"]
    campaign.payment_link_url = link["short_url"]
    campaign.approved_at = datetime.now(timezone.utc)
    session.add(campaign)

    log_action(
        session, "campaign_agent", "campaign_approved",
        actor_ref=current_user.id, user_id=current_user.id,
        rationale=f"merchant approved '{campaign.name}'; payment link minted at "
        f"{campaign.discount_pct}% off (₹{discounted}); budget cap ₹{campaign.budget_inr}",
        amount_inr=campaign.budget_inr, gate_status="approved",
        detail={"campaignId": campaign.id, "paymentLink": link["short_url"], "mock": link.get("mock", False)},
    )
    return campaign.to_dict()


@router.post("/{campaign_id}/reject")
def reject_campaign(
    campaign_id: str,
    current_user: User = Depends(require_merchant),
    session: Session = Depends(get_session),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.status != CampaignStatus.draft:
        return JSONResponse(status_code=400, content={"message": "Campaign not found or not a draft"})
    campaign.status = CampaignStatus.rejected
    session.add(campaign)
    log_action(
        session, "campaign_agent", "campaign_rejected",
        actor_ref=current_user.id, user_id=current_user.id,
        rationale=f"merchant rejected draft '{campaign.name}'; nothing went live",
        gate_status="rejected",
        detail={"campaignId": campaign.id},
    )
    return campaign.to_dict()


@router.get("")
def list_campaigns(
    current_user: User = Depends(require_merchant),
    session: Session = Depends(get_session),
):
    campaigns = session.exec(select(Campaign).order_by(Campaign.created_at.desc())).all()
    return [c.to_dict() for c in campaigns]


@router.get("/active")
def active_campaigns(session: Session = Depends(get_session)):
    """Public: active campaigns, so checkout surfaces can offer the discount."""
    campaigns = session.exec(
        select(Campaign).where(Campaign.status == CampaignStatus.active)
    ).all()
    return [c.to_dict() for c in campaigns]


@router.get("/audit/all")
def full_audit(
    current_user: User = Depends(require_merchant),
    session: Session = Depends(get_session),
    limit: int = 100,
):
    """Merchant view: the complete agent audit trail across all actors."""
    actions = session.exec(
        select(AgentAction).order_by(AgentAction.created_at.desc()).limit(min(limit, 500))
    ).all()
    return [a.to_dict() for a in actions]
