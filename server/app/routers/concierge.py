"""
Concierge endpoints: conversational checkout with an explicit human gate.

/chat only ever talks and proposes. /confirm — a button click, never the
agent — is what creates a Razorpay order. /reject closes the loop in the
audit trail when the user declines.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.commerce import concierge
from app.commerce.audit import log_action
from app.commerce.orders import BoundsExceeded, CartError, create_order, price_cart
from app.db.engine import get_session
from app.db.models import (
    AgentAction,
    ConciergeMessage,
    ConciergeSession,
    ServiceAddon,
    User,
)
from app.deps.auth import get_current_user
from app.services.razorpay_gateway import get_gateway

router = APIRouter(prefix="/api/concierge", tags=["concierge"])


def _resolve_session_id(session: Session, user: User, session_id: str) -> str:
    """If session_id belongs to a different account, mint a fresh one instead
    of reusing it -- commerce.concierge._sessions is a single process-wide
    dict keyed only by session_id, with no per-user isolation of its own (see
    app/routers/chat.py's _resolve_session_id, which this mirrors)."""
    existing = session.get(ConciergeSession, session_id)
    if existing is not None and existing.user_id != user.id:
        return str(uuid.uuid4())
    return session_id


def _persist_concierge_turn(
    session: Session,
    user: User,
    session_id: str,
    user_message: str,
    agent_reply: str,
    lawyers: list,
) -> None:
    """Create the concierge_sessions row on first turn, then append a user
    and an agent message. Mirrors app/routers/chat.py's _persist_turn,
    including the explicit strictly-increasing created_at (Postgres's now()
    returns the transaction start time for every statement in it, so both
    rows would otherwise tie) and the first-turn PK-collision recovery."""
    concierge_session = session.get(ConciergeSession, session_id)
    if concierge_session is None:
        concierge_session = ConciergeSession(
            id=session_id, user_id=user.id, title=user_message[:80]
        )
        session.add(concierge_session)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            concierge_session = session.get(ConciergeSession, session_id)
            if concierge_session is None or concierge_session.user_id != user.id:
                return

    user_turn_at = datetime.now(timezone.utc)
    session.add(
        ConciergeMessage(
            session_id=session_id,
            role="user",
            content=user_message,
            created_at=user_turn_at,
        )
    )
    session.add(
        ConciergeMessage(
            session_id=session_id,
            role="agent",
            content=agent_reply,
            meta={"lawyers": lawyers} if lawyers else None,
            created_at=user_turn_at + timedelta(microseconds=1),
        )
    )
    concierge_session.updated_at = datetime.now(timezone.utc)
    session.commit()


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
    session_id = _resolve_session_id(session, current_user, body.sessionId)
    result = await concierge.handle_turn(
        session, session_id, current_user.id, body.message.strip()
    )
    _persist_concierge_turn(
        session, current_user, session_id,
        body.message.strip(), result["reply"], result.get("lawyers") or [],
    )
    result["mock"] = get_gateway().is_mock
    result["sessionId"] = session_id
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


@router.get("/sessions")
def list_sessions(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """List the current user's persisted concierge conversations, most
    recent first (mirrors GET /api/chat/sessions)."""
    rows = session.exec(
        select(ConciergeSession)
        .where(ConciergeSession.user_id == current_user.id)
        .order_by(ConciergeSession.updated_at.desc())
    ).all()
    return {"sessions": [s.to_dict() for s in rows], "count": len(rows)}


@router.get("/session/{session_id}/history")
def get_session_history(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Full transcript for a concierge conversation the current user owns
    (mirrors GET /api/chat/session/{id}/history). Empty for a session_id
    that doesn't exist or belongs to someone else -- concierge has no guest
    in-memory fallback the way the chatbot does, since /chat always requires
    auth."""
    concierge_session = session.get(ConciergeSession, session_id)
    if concierge_session is None or concierge_session.user_id != current_user.id:
        return {"sessionId": session_id, "messages": [], "count": 0}
    rows = session.exec(
        select(ConciergeMessage)
        .where(ConciergeMessage.session_id == session_id)
        .order_by(ConciergeMessage.created_at)
    ).all()
    messages = [m.to_dict() for m in rows]
    return {"sessionId": session_id, "messages": messages, "count": len(messages)}


@router.delete("/session/{session_id}")
def clear_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Delete a concierge conversation's durable history (cascades to its
    messages) and drop its in-memory selection state. Silently succeeds for
    a session_id that doesn't exist or belongs to someone else, matching
    DELETE /api/chat/session/{id}'s can't-tell-the-difference response."""
    concierge_session = session.get(ConciergeSession, session_id)
    if concierge_session is not None and concierge_session.user_id == current_user.id:
        session.delete(concierge_session)  # cascades to concierge_messages
        session.commit()
    concierge.forget_session(session_id)
    return {"message": f"Session {session_id} cleared"}
