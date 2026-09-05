"""
Concierge endpoints: conversational checkout with an explicit human gate.

/chat only ever talks and proposes. /confirm — a button click, never the
agent — is what creates a Razorpay order. /reject closes the loop in the
audit trail when the user declines.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.commerce import concierge
from app.commerce.audit import log_action
from app.commerce.orders import BoundsExceeded, CartError, create_order, price_cart
from app.db.engine import get_session
from app.db.models import AgentAction, ServiceAddon, User
from app.deps.auth import get_current_user
from app.services.razorpay_gateway import get_gateway

router = APIRouter(prefix="/api/concierge", tags=["concierge"])


class ChatRequest(BaseModel):
    sessionId: str
    message: str


@router.post("/chat")
async def chat(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not body.message.strip():
        return JSONResponse(status_code=400, content={"message": "Empty message"})
    result = await concierge.handle_turn(
        session, body.sessionId, current_user.id, body.message.strip()
    )
    result["mock"] = get_gateway().is_mock
    return result


class ConfirmRequest(BaseModel):
    sessionId: str
    proposalId: str


@router.post("/confirm")
def confirm(
    body: ConfirmRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The human gate: approve the concierge's pending proposal and create
    the actual Razorpay order.

    Prices strictly from what the proposal showed the user (its `detail`
    snapshot), not from the live session cart — the user may keep chatting
    ("add the written opinion too") after "checkout" but before clicking
    Confirm, and the amount they approved must be the amount they're
    charged. If the cart has drifted since the proposal, reject and ask
    for a fresh proposal rather than silently charging a different total.
    """
    proposal = session.get(AgentAction, body.proposalId)
    if (
        proposal is None
        or proposal.user_id != current_user.id
        or proposal.action != "checkout_proposed"
        or proposal.gate_status != "pending"
    ):
        return JSONResponse(
            status_code=400, content={"message": "Proposal not found or already resolved."}
        )

    snapshot = proposal.detail or {}
    lawyer_id = snapshot.get("lawyerId")
    addon_ids = snapshot.get("addonIds", [])
    if not lawyer_id:
        return JSONResponse(status_code=400, content={"message": "Proposal snapshot missing — ask the concierge again."})

    try:
        cart = price_cart(session, lawyer_id, addon_ids)
    except CartError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})

    if cart.total_inr != snapshot.get("totalInr"):
        return JSONResponse(
            status_code=409,
            content={
                "message": "The cart changed since this proposal was made — ask the "
                "concierge to check out again so you approve the current total."
            },
        )

    try:
        order, rzp_order = create_order(
            session,
            cart,
            channel="concierge",
            actor="concierge",
            actor_ref=body.sessionId,
            rationale=f"user explicitly approved proposal {proposal.id} via Confirm & Pay",
            user_id=current_user.id,
            gate_status="approved",
        )
    except BoundsExceeded as e:
        return JSONResponse(status_code=400, content={"message": e.result.reason})

    proposal.gate_status = "approved"
    session.add(proposal)
    session.commit()

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


class RejectRequest(BaseModel):
    sessionId: str
    proposalId: str


@router.post("/reject")
def reject(
    body: RejectRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    proposal = session.get(AgentAction, body.proposalId)
    if proposal and proposal.user_id == current_user.id and proposal.gate_status == "pending":
        proposal.gate_status = "rejected"
        session.add(proposal)
        log_action(
            session,
            "concierge",
            "checkout_rejected",
            actor_ref=body.sessionId,
            user_id=current_user.id,
            rationale="user declined the proposed checkout at the gate; no order was created",
            amount_inr=proposal.amount_inr,
        )
    return {"status": "recorded"}


@router.get("/addons")
def list_addons(session: Session = Depends(get_session)):
    addons = session.exec(select(ServiceAddon).where(ServiceAddon.active == True)).all()  # noqa: E712
    return [a.to_dict() for a in addons]


@router.get("/audit")
def my_audit(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    limit: int = 50,
):
    """The signed-in user's own agent audit trail (newest first)."""
    actions = session.exec(
        select(AgentAction)
        .where(AgentAction.user_id == current_user.id)
        .order_by(AgentAction.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    return [a.to_dict() for a in actions]
