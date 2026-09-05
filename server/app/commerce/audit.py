"""
Audit trail writer. Every money-touching agent step — proposal, bounds
check, gate decision, order creation, payment result, refusal — lands one
row in agent_actions, so the whole flow is reconstructable after the fact.
"""

from typing import Any, Dict, Optional

from sqlmodel import Session

from app.db.models import AgentAction


def log_action(
    session: Session,
    actor: str,
    action: str,
    *,
    actor_ref: str = "",
    user_id: Optional[str] = None,
    rationale: str = "",
    amount_inr: Optional[int] = None,
    bounds_check: str = "n/a",
    gate_status: str = "not_required",
    order_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> AgentAction:
    entry = AgentAction(
        actor=actor,
        actor_ref=actor_ref,
        user_id=user_id,
        action=action,
        rationale=rationale,
        amount_inr=amount_inr,
        bounds_check=bounds_check,
        gate_status=gate_status,
        order_id=order_id,
        detail=detail or {},
    )
    session.add(entry)
    if commit:
        session.commit()
        session.refresh(entry)
    return entry
