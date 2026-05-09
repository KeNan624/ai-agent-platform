"""
Permission & quota enforcement for the AI Agent Platform.

Plan rules
----------
Quotas stay in code for now. Allowed models are read from CHAT_MODEL_CONFIG
in app_settings so the admin panel can change them at runtime.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from chat_model_config import get_available_model_ids, is_model_allowed_for_plan
from models import Membership, UsageLog, User

# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------

PLAN_RULES: dict[str, dict] = {
    "free": {
        "quota": 10,
        "period": "day",
    },
    "monthly": {
        "quota": 500,
        "period": "month",
    },
    "yearly": {
        "quota": 1000,
        "period": "month",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_active_plan(user: User, db: Session) -> str:
    """Return the user's current active plan type, defaulting to 'free'."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    membership = (
        db.query(Membership)
        .filter(
            Membership.user_id == user.id,
            Membership.status == "active",
            (Membership.expire_at == None) | (Membership.expire_at > now),
        )
        .order_by(Membership.expire_at.desc().nullslast())
        .first()
    )
    if membership is None:
        return "free"
    return membership.plan_type


def get_active_membership(user: User, db: Session) -> Optional[Membership]:
    """Return the active Membership row, or None for free users."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (
        db.query(Membership)
        .filter(
            Membership.user_id == user.id,
            Membership.status == "active",
            (Membership.expire_at == None) | (Membership.expire_at > now),
        )
        .order_by(Membership.expire_at.desc().nullslast())
        .first()
    )


def _count_usage(user_id: int, period: str, db: Session) -> int:
    """Count usage_logs rows for the current day or month."""
    now = datetime.utcnow()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return (
        db.query(func.count(UsageLog.id))
        .filter(UsageLog.user_id == user_id, UsageLog.created_at >= start)
        .scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_permission(user: User, model: str, db: Session) -> str:
    """
    Validate model access and quota for the given user.

    Returns the active plan_type on success.
    Raises HTTPException 403 with error_code on failure.
    """
    plan_type = get_active_plan(user, db)
    rules = PLAN_RULES.get(plan_type, PLAN_RULES["free"])

    # 1. Model access check
    allowed_models = get_available_model_ids(plan_type, db)
    if not is_model_allowed_for_plan(model, plan_type, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "MODEL_NOT_ALLOWED",
                "message": (
                    f"Model '{model}' is not available on the '{plan_type}' plan. "
                    f"Allowed models: {allowed_models}"
                ),
            },
        )

    # 2. Quota check
    used = _count_usage(user.id, rules["period"], db)
    if used >= rules["quota"]:
        period_label = "today" if rules["period"] == "day" else "this month"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "QUOTA_EXCEEDED",
                "message": (
                    f"You have used {used}/{rules['quota']} calls {period_label} "
                    f"on the '{plan_type}' plan."
                ),
            },
        )

    return plan_type


def record_usage(user: User, model: str, db: Session, message_count: int = 1) -> None:
    """Write one UsageLog row after a successful chat call."""
    log = UsageLog(
        user_id=user.id,
        model=model,
        message_count=message_count,
        created_at=datetime.utcnow(),
    )
    db.add(log)
    db.commit()


def get_quota_status(user: User, db: Session) -> dict:
    """Return a dict with full quota info for /member/info."""
    plan_type = get_active_plan(user, db)
    rules = PLAN_RULES.get(plan_type, PLAN_RULES["free"])
    membership = get_active_membership(user, db)

    used = _count_usage(user.id, rules["period"], db)
    remaining = max(0, rules["quota"] - used)

    return {
        "plan_type": plan_type,
        "expire_at": membership.expire_at if membership else None,
        "period": rules["period"],
        "quota": rules["quota"],
        "used": used,
        "remaining": remaining,
        "allowed_models": get_available_model_ids(plan_type, db),
    }
