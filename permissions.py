"""
Permission and credit enforcement for the AI Agent Platform.

Plan rules
----------
Plan quota values are treated as periodic credit grants. Model access,
feature access and practice permissions are read from PLAN_CONFIG in
app_settings so the admin panel can change them at runtime.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from chat_model_config import get_available_model_ids, is_model_allowed_for_plan
from credit_config import get_feature_credit_price, get_model_credit_price
from credit_service import (
    InsufficientCreditsError,
    grant_credits,
    consume_credits,
    get_credit_balance,
    get_credit_summary,
    has_enough_credits,
)
from models import Membership, UsageLog, User
from plan_config import (
    get_enabled_features_for_plan,
    get_feature_label,
    get_free_model_ids_for_plan,
    get_plan_definition,
    is_model_free_for_plan,
    plan_has_feature,
)

FEATURE_USAGE_PREFIX = "feature:"
PAYMENT_REQUIRED = getattr(status, "HTTP_402_PAYMENT_REQUIRED", 402)


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


def get_active_plan_for_user_id(user_id: int, db: Session) -> str:
    """Return the current active plan type for a user id."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    membership = (
        db.query(Membership)
        .filter(
            Membership.user_id == int(user_id),
            Membership.status == "active",
            (Membership.expire_at == None) | (Membership.expire_at > now),
        )
        .order_by(Membership.expire_at.desc().nullslast())
        .first()
    )
    return membership.plan_type if membership else "free"


def _period_credit_source_id(user_id: int, plan_type: str, period: str) -> str:
    now = datetime.now(timezone.utc)
    key = now.strftime("%Y%m%d") if period == "day" else now.strftime("%Y%m")
    return f"{int(user_id)}:{plan_type}:{key}"


def ensure_plan_period_credits(user: User, db: Session) -> bool:
    return ensure_plan_period_credits_for_user_id(int(user.id), db, get_active_plan(user, db))


def ensure_plan_period_credits_for_user_id(user_id: int, db: Session, plan_type: Optional[str] = None) -> bool:
    plan_key = plan_type or get_active_plan_for_user_id(int(user_id), db)
    plan = get_plan_definition(plan_key, db)
    amount = Decimal(str(plan.get("quota") or 0))
    if amount <= 0:
        return False
    period = str(plan.get("period") or "month").strip().lower()
    source_id = _period_credit_source_id(int(user_id), plan_key, period)
    granted = grant_credits(
        db,
        user_id=int(user_id),
        amount=amount,
        source_type="plan_period",
        source_id=source_id,
        description=f"{plan.get('name') or plan_key}周期赠送积分",
    )
    if granted:
        db.commit()
    return granted


def _feature_denied(feature_key: str, plan_type: str, db: Session) -> HTTPException:
    feature_label = get_feature_label(feature_key)
    plan = get_plan_definition(plan_type, db)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "FEATURE_NOT_ALLOWED",
            "feature": feature_key,
            "message": f"当前套餐「{plan.get('name') or plan_type}」暂不支持{feature_label}",
        },
    )

def _insufficient_credits(item_type: str, item_key: str, required: int, user_id: int, db: Session) -> HTTPException:
    balance = get_credit_balance(user_id, db)
    return HTTPException(
        status_code=PAYMENT_REQUIRED,
        detail={
            "error_code": "INSUFFICIENT_CREDITS",
            "item_type": item_type,
            "item_key": item_key,
            "required_credits": required,
            "credit_balance": str(balance),
            "message": f"积分余额不足，本次需要 {required} 积分，当前余额 {balance} 积分",
        },
    )


def _credit_allowed(user_id: int, item_type: str, item_key: str, db: Session) -> bool:
    if item_type == "model":
        price = get_model_credit_price(item_key, db)
    else:
        price = get_feature_credit_price(item_key, db)
    return price > 0 and has_enough_credits(user_id, price, db)


def _require_credit_or_raise(user_id: int, item_type: str, item_key: str, db: Session) -> None:
    price = get_model_credit_price(item_key, db) if item_type == "model" else get_feature_credit_price(item_key, db)
    if price <= 0:
        return
    if not has_enough_credits(user_id, price, db):
        raise _insufficient_credits(item_type, item_key, price, user_id, db)


def require_plan_feature(user: User, feature_key: str, db: Session) -> str:
    """Ensure the user's active plan enables a non-chat platform feature."""
    ensure_plan_period_credits(user, db)
    plan_type = get_active_plan(user, db)
    if not plan_has_feature(plan_type, feature_key, db):
        raise _feature_denied(feature_key, plan_type, db)
    _require_credit_or_raise(int(user.id), "feature", feature_key, db)
    return plan_type


def require_plan_feature_for_user_id(user_id: int, feature_key: str, db: Session) -> str:
    ensure_plan_period_credits_for_user_id(int(user_id), db)
    plan_type = get_active_plan_for_user_id(user_id, db)
    if not plan_has_feature(plan_type, feature_key, db):
        raise _feature_denied(feature_key, plan_type, db)
    _require_credit_or_raise(int(user_id), "feature", feature_key, db)
    return plan_type


def _count_usage(user_id: int, period: str, db: Session) -> int:
    """Count quota-charged usage_logs rows for the current day or month."""
    now = datetime.utcnow()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return (
        db.query(func.count(UsageLog.id))
        .filter(
            UsageLog.user_id == user_id,
            UsageLog.created_at >= start,
            UsageLog.quota_charged == True,  # noqa: E712
        )
        .scalar()
        or 0
    )


def _count_feature_usage(user_id: int, feature_key: str, period: str, db: Session) -> int:
    """Count successful feature API calls for the current day or month."""
    now = datetime.utcnow()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return (
        db.query(func.count(UsageLog.id))
        .filter(
            UsageLog.user_id == user_id,
            UsageLog.created_at >= start,
            UsageLog.model == f"{FEATURE_USAGE_PREFIX}{feature_key}",
        )
        .scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_permission(user: User, model: str, db: Session) -> str:
    """
    Validate model access and credit balance for the given user.

    Returns the active plan_type on success.
    Raises HTTPException 403 with error_code on failure.
    """
    ensure_plan_period_credits(user, db)
    plan_type = get_active_plan(user, db)

    # 1. Model access check.
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

    # 2. Credit check. Free models are explicit no-credit models; all other
    # enabled models charge the configured credit price.
    if is_model_free_for_plan(plan_type, model, db):
        return plan_type
    _require_credit_or_raise(int(user.id), "model", model, db)
    return plan_type


def _record_credit_usage(
    *,
    db: Session,
    user_id: int,
    price: int,
    source_id: str,
    item_type: str,
    item_key: str,
) -> None:
    consume_credits(
        db,
        user_id=int(user_id),
        amount=price,
        source_type="usage_log",
        source_id=source_id,
        item_type=item_type,
        item_key=item_key,
        description=f"{item_key} 使用扣费",
    )


def record_usage(user: User, model: str, db: Session, message_count: int = 1) -> None:
    """Write one UsageLog row after a successful chat call."""
    plan_type = get_active_plan(user, db)
    credit_price = 0

    if not is_model_free_for_plan(plan_type, model, db):
        credit_price = get_model_credit_price(model, db)

    log = UsageLog(
        user_id=user.id,
        model=model,
        message_count=message_count,
        quota_charged=False,
        created_at=datetime.utcnow(),
    )
    try:
        db.add(log)
        db.flush()
        if credit_price > 0:
            _record_credit_usage(
                db=db,
                user_id=int(user.id),
                price=credit_price,
                source_id=str(log.id),
                item_type="model",
                item_key=model,
            )
        db.commit()
    except InsufficientCreditsError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _feature_credit_price_if_needed(user_id: int, plan_type: str, feature_key: str, db: Session) -> int:
    return get_feature_credit_price(feature_key, db)


def record_feature_usage(user: User, feature_key: str, db: Session) -> None:
    """Write one UsageLog row after a successful non-chat feature call."""
    plan_type = get_active_plan(user, db)
    credit_price = _feature_credit_price_if_needed(int(user.id), plan_type, feature_key, db)
    log = UsageLog(
        user_id=user.id,
        model=f"{FEATURE_USAGE_PREFIX}{feature_key}",
        message_count=1,
        quota_charged=False,
        created_at=datetime.utcnow(),
    )
    try:
        db.add(log)
        db.flush()
        if credit_price > 0:
            _record_credit_usage(
                db=db,
                user_id=int(user.id),
                price=credit_price,
                source_id=str(log.id),
                item_type="feature",
                item_key=feature_key,
            )
        db.commit()
    except InsufficientCreditsError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def record_feature_usage_for_user_id(user_id: int, feature_key: str, db: Session) -> None:
    """Write one feature UsageLog row when only a user id is available."""
    plan_type = get_active_plan_for_user_id(int(user_id), db)
    credit_price = _feature_credit_price_if_needed(int(user_id), plan_type, feature_key, db)
    log = UsageLog(
        user_id=int(user_id),
        model=f"{FEATURE_USAGE_PREFIX}{feature_key}",
        message_count=1,
        quota_charged=False,
        created_at=datetime.utcnow(),
    )
    try:
        db.add(log)
        db.flush()
        if credit_price > 0:
            _record_credit_usage(
                db=db,
                user_id=int(user_id),
                price=credit_price,
                source_id=str(log.id),
                item_type="feature",
                item_key=feature_key,
            )
        db.commit()
    except InsufficientCreditsError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def get_quota_status(user: User, db: Session) -> dict:
    """Return a dict with full quota info for /member/info."""
    ensure_plan_period_credits(user, db)
    plan_type = get_active_plan(user, db)
    plan = get_plan_definition(plan_type, db)
    membership = get_active_membership(user, db)

    used = _count_usage(user.id, plan["period"], db)
    feature_usage = {
        key: _count_feature_usage(user.id, key, plan["period"], db)
        for key in get_enabled_features_for_plan(plan_type, db)
    }

    return {
        "plan_type": plan_type,
        "plan_name": plan["name"],
        "expire_at": membership.expire_at if membership else None,
        "period": plan["period"],
        "quota": plan["quota"],
        "used": used,
        "remaining": int(get_credit_balance(int(user.id), db)),
        "allowed_models": get_available_model_ids(plan_type, db),
        "free_models": get_free_model_ids_for_plan(plan_type, db),
        "enabled_features": get_enabled_features_for_plan(plan_type, db),
        "feature_quotas": {},
        "feature_usage": feature_usage,
        "practice_access": bool(plan.get("practice_access")),
        "practice_publish": bool(plan.get("practice_publish")),
        **get_credit_summary(user, db),
    }
