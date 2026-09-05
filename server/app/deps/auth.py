"""
Shared per-request auth dependency. Before this module, only GET /api/auth/me
verified the caller's JWT — every other route trusted a raw userId/lawyerId
field in the request body with no verification at all. New protected routes
should depend on get_current_user instead of repeating that pattern.
"""

from typing import Optional

import jwt
from fastapi import Depends, Header
from sqlmodel import Session

from app.config import get_settings
from app.db.engine import get_session
from app.db.models import User
from app.deps.errors import MessageHTTPException
from app.security import jwt_secret


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    token = (
        authorization.split(" ")[1]
        if authorization and " " in authorization
        else None
    )
    if not token:
        raise MessageHTTPException(status_code=401, detail="No token provided")

    try:
        decoded = jwt.decode(token, jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise MessageHTTPException(status_code=401, detail="Invalid token")

    user = session.get(User, decoded.get("id"))
    if not user:
        raise MessageHTTPException(status_code=401, detail="User not found")

    return user


def get_current_user_optional(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> Optional[User]:
    """Same as get_current_user but returns None instead of raising, for
    routes that behave differently for anonymous vs. logged-in callers
    (e.g. attaching search history only when a user is present)."""
    if not authorization:
        return None
    try:
        return get_current_user(authorization=authorization, session=session)
    except MessageHTTPException:
        return None


def require_merchant(user: User = Depends(get_current_user)) -> User:
    """Gate for merchant-only routes (campaign approval, full audit trail —
    minting live discounts/links and reading the cross-user agent audit
    trail).

    MERCHANT_EMAILS is an allowlist. With it unset, this fails *closed*
    (403 for everyone) except in local debug mode, where it's a no-op so a
    single-operator buildathon demo or the evaluation harness can run
    without provisioning a merchant account. An unset allowlist that
    defaulted to "let any authenticated user in" would be a real
    privilege-escalation gap the moment this runs anywhere but a laptop.
    Set MERCHANT_EMAILS in .env to restrict these routes to specific
    accounts in any non-debug deployment.
    """
    settings = get_settings()
    allowlist = [e.strip().lower() for e in settings.merchant_emails.split(",") if e.strip()]
    if not allowlist:
        if settings.debug:
            return user
        raise MessageHTTPException(
            status_code=403,
            detail="Merchant access required (no MERCHANT_EMAILS configured)",
        )
    if user.email.lower() not in allowlist:
        raise MessageHTTPException(status_code=403, detail="Merchant access required")
    return user
