"""
LexCart concierge: conversational in-app checkout agent.

Architecture (deliberately hybrid for reliability on a small local LLM):
- the LLM does language understanding (one strict-JSON intent parse per
  turn, with a keyword fallback) and free-text answers;
- everything that touches money is deterministic code: catalog search,
  cart state, pricing, upsell selection, bounds checks, templated amounts.

The agent can only ever PROPOSE a checkout. The proposal is audited with
gate_status=pending; an order is created solely by the /confirm endpoint,
i.e. an explicit human click — that is the gate.
"""

import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.commerce.audit import log_action
from app.commerce.guardrails import check_order_bounds
from app.commerce.orders import CartError, PricedCart, price_cart
from app.config import get_settings
from app.db.models import Lawyer, ServiceAddon
from app.tools.lawyer_recommender import recommend_lawyers


@dataclass
class ConciergeSession:
    user_id: str
    lawyer_id: Optional[str] = None
    addon_ids: List[str] = field(default_factory=list)
    last_results: List[str] = field(default_factory=list)  # lawyer ids last shown
    updated_at: float = field(default_factory=time.time)


# An OrderedDict guarded by a plain threading.Lock, not an asyncio.Lock:
# this dict is mutated both from the async handle_turn (event-loop thread)
# and from the sync clear_session endpoint (a FastAPI threadpool thread),
# and an asyncio.Lock only serializes coroutines on one loop, not real
# threads. move_to_end on every touch + popitem(last=False) on eviction
# gives LRU semantics instead of the previous FIFO-by-insertion-order
# eviction, which could drop an actively-used session while an idle one
# survived.
_sessions: "OrderedDict[str, ConciergeSession]" = OrderedDict()
_sessions_lock = threading.Lock()


def _get_session_state(session_id: str, user_id: str) -> ConciergeSession:
    settings = get_settings()
    now = time.time()
    with _sessions_lock:
        expired = [
            sid
            for sid, st in _sessions.items()
            if now - st.updated_at > settings.session_ttl_seconds
        ]
        for sid in expired:
            _sessions.pop(sid, None)
        while len(_sessions) >= settings.max_sessions:
            _sessions.popitem(last=False)
        state = _sessions.get(session_id)
        if state is None or state.user_id != user_id:
            state = ConciergeSession(user_id=user_id)
            _sessions[session_id] = state
        else:
            _sessions.move_to_end(session_id)
        state.updated_at = now
        return state


def forget_session(session_id: str) -> None:
    """Drop a session's in-memory selection state (lawyer/addons/last
    results). Used when a conversation's durable history row is deleted, so
    a since-deleted session_id doesn't keep silently reusing stale state."""
    with _sessions_lock:
        _sessions.pop(session_id, None)


# ── Intent parsing ──────────────────────────────────────────────────────────

_PARSE_PROMPT = """You are the intent parser for a legal-services shopping assistant.
Classify the user's message into exactly one intent and extract slots.

Intents:
- find_lawyer: user describes a legal need or asks to see/browse lawyers
- choose_lawyer: user picks a lawyer (by name, or "the first one", "option 2")
- add_addon: user wants an extra service (document review, follow-up call, written opinion, notice draft)
- remove_addon: user wants to drop an extra service
- checkout: user wants to book/pay/proceed to payment
- show_cart: user asks what's in their cart / the total
- question: any other question
Reply with ONLY a JSON object, no prose:
{{"intent": "...", "topic": string|null, "specialty": string|null, "budget_inr": number|null, "lawyer_ref": string|null, "addon_ref": string|null}}

specialty must be one of: "Criminal Defense", "Family Law", "Business & Corporate Law", "Personal Injury", "Real Estate Law", or null.
lawyer_ref: the name or ordinal the user used, verbatim. addon_ref: the addon words used.

User message: "{message}"
JSON:"""

_ADDON_KEYWORDS = {
    "review": "addon-doc-review",
    "document": "addon-doc-review",
    "follow": "addon-followup-call",
    "call": "addon-followup-call",
    "opinion": "addon-written-opinion",
    "written": "addon-written-opinion",
    "notice": "addon-notice-draft",
    "draft": "addon-notice-draft",
}

_SPECIALTY_KEYWORDS = {
    "Criminal Defense": ["criminal", "bail", "fir", "arrest", "theft", "assault"],
    "Family Law": ["divorce", "custody", "family", "marriage", "maintenance", "alimony"],
    "Business & Corporate Law": ["business", "corporate", "startup", "company", "contract", "partnership"],
    "Personal Injury": ["accident", "injury", "insurance", "compensation", "mact"],
    "Real Estate Law": ["property", "real estate", "land", "tenant", "rent", "rera", "title"],
}


def _fallback_parse(message: str) -> Dict[str, Any]:
    """Deterministic parse used when the LLM is unavailable or emits
    unusable JSON — keeps the demo alive no matter what."""
    m = message.lower()
    slots: Dict[str, Any] = {
        "intent": "question",
        "topic": message,
        "specialty": None,
        "budget_inr": None,
        "lawyer_ref": None,
        "addon_ref": None,
    }
    budget = re.search(r"(?:under|below|within|max|budget of?)\s*(?:rs\.?|₹|inr)?\s*([\d,]{3,})", m)
    if budget:
        slots["budget_inr"] = int(budget.group(1).replace(",", ""))
    for specialty, words in _SPECIALTY_KEYWORDS.items():
        if any(w in m for w in words):
            slots["specialty"] = specialty
            break
    if any(w in m for w in ("checkout", "book", "pay", "proceed", "confirm")):
        slots["intent"] = "checkout"
    elif any(w in m for w in ("cart", "total", "summary")):
        slots["intent"] = "show_cart"
    elif any(w in m for w in ("remove", "drop", "cancel the", "without")) and any(
        w in m for w in _ADDON_KEYWORDS
    ):
        slots["intent"] = "remove_addon"
        slots["addon_ref"] = m
    elif any(w in m for w in ("add", "include", "also", "yes")) and any(
        w in m for w in _ADDON_KEYWORDS
    ):
        slots["intent"] = "add_addon"
        slots["addon_ref"] = m
    elif re.search(r"(first|second|third|option\s*\d|number\s*\d|choose|select|go with|pick)", m):
        slots["intent"] = "choose_lawyer"
        slots["lawyer_ref"] = message
    elif slots["specialty"] or any(w in m for w in ("lawyer", "advocate", "consult", "help with")):
        slots["intent"] = "find_lawyer"
    return slots


async def _parse_intent(message: str) -> Dict[str, Any]:
    try:
        from app.chatbot import get_fast_llm, invoke_llm_safely, strip_reasoning_tags

        raw = await invoke_llm_safely(
            get_fast_llm(), _PARSE_PROMPT.format(message=message[:500]), stream=False,
            timeout_seconds=20.0,
        )
        raw = strip_reasoning_tags(raw)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if parsed.get("intent") in (
                "find_lawyer", "choose_lawyer", "add_addon",
                "remove_addon", "checkout", "show_cart", "question",
            ):
                return parsed
    except Exception as e:
        print(f"[Concierge] LLM parse failed, using fallback: {e}")
    return _fallback_parse(message)


async def _parse_with_merge(message: str) -> Dict[str, Any]:
    """LLM parse, with the deterministic parser filling any slot the LLM
    left null — a small local model frequently classifies the intent right
    but drops the budget/specialty details."""
    slots = await _parse_intent(message)
    fallback = _fallback_parse(message)
    for key in ("topic", "specialty", "budget_inr", "lawyer_ref", "addon_ref"):
        if not slots.get(key) and fallback.get(key):
            slots[key] = fallback[key]
    return slots


# ── Helpers ─────────────────────────────────────────────────────────────────

_ORDINALS = {"first": 0, "1": 0, "one": 0, "second": 1, "2": 1, "two": 1, "third": 2, "3": 2, "three": 2}


def _resolve_lawyer(db: Session, state: ConciergeSession, ref: str) -> Optional[Lawyer]:
    ref_l = (ref or "").lower()
    for word, idx in _ORDINALS.items():
        if word in ref_l and idx < len(state.last_results):
            return db.get(Lawyer, state.last_results[idx])
    # Try the lawyers we actually just showed before scanning the whole
    # table -- "go with Priya" / "the criminal lawyer" almost always names
    # one of last_results, not some other lawyer never mentioned yet.
    shown = [lw for lid in state.last_results if (lw := db.get(Lawyer, lid)) is not None]
    for lawyer in shown:
        parts = lawyer.name.lower().split()
        if lawyer.name.lower() in ref_l or any(p in ref_l for p in parts if len(p) > 3):
            return lawyer
    if len(shown) == 1:
        return shown[0]
    for lawyer in db.exec(select(Lawyer)).all():
        parts = lawyer.name.lower().split()
        if lawyer.name.lower() in ref_l or any(p in ref_l for p in parts if len(p) > 3):
            return lawyer
    return None


def _resolve_addons(db: Session, ref: str) -> List[ServiceAddon]:
    ref_l = (ref or "").lower()
    ids = {aid for word, aid in _ADDON_KEYWORDS.items() if word in ref_l}
    return [a for aid in ids if (a := db.get(ServiceAddon, aid)) and a.active]


def _relevant_addons(db: Session, lawyer: Lawyer, exclude: List[str]) -> List[ServiceAddon]:
    addons = db.exec(select(ServiceAddon).where(ServiceAddon.active == True)).all()  # noqa: E712
    return [
        a
        for a in addons
        if a.id not in exclude and (not a.applies_to or lawyer.specialty in a.applies_to)
    ]


def _to_cart_payload(cart: PricedCart) -> Dict[str, Any]:
    return {
        "lawyer": cart.lawyer.to_dict(),
        "addons": [a.to_dict() for a in cart.addons],
        "lineItems": cart.line_items(),
        "totalInr": cart.total_inr,
    }


def _cart_payload(db: Session, state: ConciergeSession) -> Optional[Dict[str, Any]]:
    if not state.lawyer_id:
        return None
    try:
        cart = price_cart(db, state.lawyer_id, state.addon_ids)
    except CartError:
        return None
    return _to_cart_payload(cart)


# ── Main turn handler ───────────────────────────────────────────────────────


async def handle_turn(
    db: Session, session_id: str, user_id: str, message: str
) -> Dict[str, Any]:
    state = _get_session_state(session_id, user_id)
    slots = await _parse_with_merge(message)
    intent = slots.get("intent", "question")

    reply = ""
    lawyers_out: List[dict] = []
    proposal = None
    suggestions: List[str] = []
    # Set by any branch that already priced the cart for its own reply, so
    # the final return doesn't re-price it a second time from scratch.
    cart_payload: Optional[Dict[str, Any]] = None

    if intent == "find_lawyer":
        try:
            results = await recommend_lawyers(
                db,
                problem_description=slots.get("topic") or message,
                specialty=slots.get("specialty"),
                max_hourly_rate=slots.get("budget_inr"),
                limit=3,
            )
            if not results:
                # Semantic search can come up empty (e.g. embeddings not
                # backfilled); retry on structured filters alone before
                # falling back to an unfiltered answer. Kept inside the
                # same try block as the first call so a semantic-search
                # outage (Lite mode / missing embedding model) can't crash
                # this retry too -- both attempts share one fallback path.
                results = await recommend_lawyers(
                    db,
                    specialty=slots.get("specialty"),
                    max_hourly_rate=slots.get("budget_inr"),
                    limit=3,
                )
        except Exception as e:
            print(f"[Concierge] semantic search unavailable: {e}")
            results = []
        if not results:
            results = list(db.exec(select(Lawyer).order_by(Lawyer.rating.desc()).limit(3)).all())
        state.last_results = [lw.id for lw in results]
        lawyers_out = [lw.to_dict() for lw in results]
        log_action(
            db, "concierge", "catalog_searched",
            actor_ref=session_id, user_id=user_id,
            rationale=f"user asked: {message[:200]!r}; matched {len(results)} lawyers"
            + (f" (specialty={slots.get('specialty')})" if slots.get("specialty") else ""),
            detail={"lawyerIds": state.last_results},
        )
        names = ", ".join(f"{i+1}. {l.name} ({l.specialty}, ₹{l.hourly_rate})" for i, l in enumerate(results))
        reply = (
            f"Here are the best matches for you: {names}. "
            "Say which one you'd like — for example, \"go with the first one\"."
        )
        suggestions = [f"Go with {results[0].name.split()[0]}", "Show me cheaper options"]

    elif intent == "choose_lawyer":
        lawyer = _resolve_lawyer(db, state, slots.get("lawyer_ref") or message)
        if lawyer is None:
            reply = "I couldn't tell which lawyer you meant — could you say the name or 'the first one'?"
        else:
            state.lawyer_id = lawyer.id
            upsells = _relevant_addons(db, lawyer, state.addon_ids)[:2]
            upsell_txt = ""
            if upsells:
                upsell_txt = " You can also add " + " or ".join(
                    f"{a.name} (₹{a.price_inr})" for a in upsells
                ) + " — many clients find these useful."
                log_action(
                    db, "concierge", "upsell_proposed",
                    actor_ref=session_id, user_id=user_id,
                    rationale=f"suggested {[a.name for a in upsells]} as relevant to a "
                    f"{lawyer.specialty} consultation; user has not accepted anything yet",
                    detail={"addonIds": [a.id for a in upsells]},
                )
            reply = (
                f"Great choice — {lawyer.name}, {lawyer.specialty}, ₹{lawyer.hourly_rate} per "
                f"consultation.{upsell_txt} Say 'checkout' whenever you're ready."
            )
            suggestions = [f"Add {upsells[0].name}" if upsells else "Checkout", "Checkout"]

    elif intent == "add_addon":
        if not state.lawyer_id:
            reply = "Let's pick a lawyer first — tell me what you need help with."
        else:
            addons = _resolve_addons(db, slots.get("addon_ref") or message)
            if not addons:
                reply = "I couldn't match that to an add-on. Options: Document Review (₹499), Follow-up Call (₹299), Written Legal Opinion (₹999), Legal Notice Draft (₹799)."
            else:
                for a in addons:
                    if a.id not in state.addon_ids:
                        state.addon_ids.append(a.id)
                added = " and ".join(a.name for a in addons)
                cart_payload = _cart_payload(db, state)
                reply = f"Added {added}. Your total is now ₹{cart_payload['totalInr']}. Say 'checkout' when ready."
                suggestions = ["Checkout", "Show my cart"]

    elif intent == "remove_addon":
        addons = _resolve_addons(db, slots.get("addon_ref") or message)
        for a in addons:
            if a.id in state.addon_ids:
                state.addon_ids.remove(a.id)
        cart_payload = _cart_payload(db, state)
        total_txt = f" Total is now ₹{cart_payload['totalInr']}." if cart_payload else ""
        reply = f"Done, removed.{total_txt}"

    elif intent == "show_cart":
        cart_payload = _cart_payload(db, state)
        if not cart_payload:
            reply = "Your cart is empty. Tell me what legal help you need and I'll find the right lawyer."
        else:
            lines = "; ".join(f"{li['label']}: ₹{li['amountInr']}" for li in cart_payload["lineItems"])
            reply = f"Your cart: {lines}. Total ₹{cart_payload['totalInr']}. Say 'checkout' to proceed."
            suggestions = ["Checkout"]

    elif intent == "checkout":
        cart_obj = None
        if not state.lawyer_id:
            reply = "There's nothing to check out yet — tell me what you need and I'll find a lawyer."
        else:
            try:
                cart_obj = price_cart(db, state.lawyer_id, state.addon_ids)
            except CartError as e:
                reply = (
                    f"Your cart can't be checked out as-is ({e}) — an item may have "
                    "become unavailable. Try removing it, or pick a different lawyer."
                )
        if cart_obj is not None:
            cart_payload = _to_cart_payload(cart_obj)
            bounds = check_order_bounds(db, cart_obj.total_inr, "concierge", user_id=user_id)
            if not bounds.ok:
                log_action(
                    db, "concierge", "checkout_blocked",
                    actor_ref=session_id, user_id=user_id,
                    rationale=bounds.reason, amount_inr=cart_obj.total_inr,
                    bounds_check="blocked", gate_status="not_required",
                    detail={"rule": bounds.rule},
                )
                reply = (
                    f"I can't propose this checkout: {bounds.reason}. "
                    "You can remove add-ons, pick a lower-rate lawyer, or complete it "
                    "yourself from the lawyer's profile page (human checkouts aren't agent-capped)."
                )
            else:
                action = log_action(
                    db, "concierge", "checkout_proposed",
                    actor_ref=session_id, user_id=user_id,
                    rationale=f"user asked to check out; proposing ₹{cart_obj.total_inr} "
                    f"({bounds.reason}). Awaiting explicit user confirmation — the agent "
                    "cannot create the order itself.",
                    amount_inr=cart_obj.total_inr,
                    bounds_check="passed", gate_status="pending",
                    detail={
                        "lineItems": cart_obj.line_items(),
                        "lawyerId": state.lawyer_id,
                        "addonIds": list(state.addon_ids),
                        "totalInr": cart_obj.total_inr,
                    },
                )
                proposal = {
                    "proposalId": action.id,
                    "lineItems": cart_obj.line_items(),
                    "totalInr": cart_obj.total_inr,
                    "boundsNote": bounds.reason,
                }
                lines = "; ".join(f"{li['label']}: ₹{li['amountInr']}" for li in cart_obj.line_items())
                reply = (
                    f"Here's your order — {lines}. Total ₹{cart_obj.total_inr}. "
                    "Hit Confirm & Pay below to approve it; I can't charge anything without your click."
                )

    else:  # question / smalltalk
        reply = await _answer_question(message)
        suggestions = ["Find me a lawyer", "Show my cart"]

    return {
        "reply": reply,
        "lawyers": lawyers_out,
        "cart": cart_payload if cart_payload is not None else _cart_payload(db, state),
        "proposal": proposal,
        "suggestions": suggestions,
    }


async def _answer_question(message: str) -> str:
    prompt = (
        "You are LexCart's shopping concierge for an Indian legal-services "
        "marketplace. Answer briefly (2-3 sentences), warmly, and steer toward "
        "finding a lawyer or booking a consultation. For substantive legal "
        "questions, recommend the site's Ask AI page and a consultation. "
        f"User: {message[:400]}"
    )
    try:
        from app.chatbot import get_fast_llm, invoke_llm_safely, strip_reasoning_tags

        answer = strip_reasoning_tags(
            await invoke_llm_safely(get_fast_llm(), prompt, stream=False, timeout_seconds=20.0)
        )
        if answer.strip():
            return answer.strip()
    except Exception as e:
        print(f"[Concierge] answer LLM failed: {e}")
    return (
        "I'm LexCart's booking concierge — tell me what legal matter you need help "
        "with (for example, 'I need help with a property dispute under ₹4000') and "
        "I'll line up the right lawyer and handle the booking."
    )


def clear_cart(session_id: str) -> None:
    with _sessions_lock:
        state = _sessions.get(session_id)
        if state:
            state.lawyer_id = None
            state.addon_ids = []
