"""
Admin router — operations tooling.

Endpoints
---------
POST /admin/login              Verify ADMIN_TOKEN, return it for frontend use
GET  /admin/stats              Dashboard stats
GET  /admin/users              List/search users with membership info
GET  /admin/orders             Recent orders list
POST /admin/grant              Manually activate membership
POST /admin/revoke             Deactivate a user's active membership
"""

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, List, Optional

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app_config import get_api_config_items, get_app_setting, save_api_config
from chat_model_config import (
    get_chat_model_config,
    get_chat_provider_status,
    save_chat_model_config,
)
from credit_config import (
    get_credit_billing_config,
    get_credit_package_config,
    save_credit_billing_config,
    save_credit_package_config,
)
from database import get_db
from models import Credit, Membership, Order, PracticeCourse, UsageLog, User
from plan_config import (
    get_feature_definitions,
    get_grantable_plans,
    get_plan_config,
    normalize_plan_config,
    save_plan_config,
)

load_dotenv()

router = APIRouter(prefix="/admin", tags=["admin"])

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")

bearer_scheme = HTTPBearer()

# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

def _token_is_admin(token: str, db: Session) -> bool:
    admin_token = os.getenv("ADMIN_TOKEN", "")
    if admin_token and token == admin_token:
        return True
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("role") == "admin":
            return True
        user_id = payload.get("sub")
        if user_id:
            user = db.query(User).filter(User.id == int(user_id)).first()
            return bool(user and getattr(user, "is_admin", False))
    except (JWTError, ValueError, TypeError):
        return False
    return False


def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> None:
    if _token_is_admin(credentials.credentials, db):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    token: str


class LoginResponse(BaseModel):
    token: str
    message: str


class StatsResponse(BaseModel):
    total_users: int
    paid_members: int
    today_new_users: int
    today_conversations: int


class UsageTotals(BaseModel):
    active_users: int
    usage_count: int
    message_count: int
    avg_daily_active_users: float


class UsageModelStat(BaseModel):
    model: str
    usage_count: int


class UsageTopUser(BaseModel):
    user_id: int
    phone: str
    nickname: Optional[str]
    usage_count: int
    message_count: int
    last_active_at: datetime


class DailyUsageStat(BaseModel):
    day: str
    active_users: int
    usage_count: int
    message_count: int
    models: list[UsageModelStat]
    top_users: list[UsageTopUser]


class UsageStatsResponse(BaseModel):
    timezone: str
    start_date: str
    end_date: str
    totals: UsageTotals
    days: list[DailyUsageStat]


class MembershipInfo(BaseModel):
    plan_type: Optional[str]
    expire_at: Optional[datetime]
    status: Optional[str]


class UserItem(BaseModel):
    id: int
    phone: str
    nickname: Optional[str]
    created_at: datetime
    is_active: bool
    membership: Optional[MembershipInfo]
    credit_balance: str = "0.00"

    class Config:
        from_attributes = True


class UsersResponse(BaseModel):
    total: int
    items: List[UserItem]


class OrderItem(BaseModel):
    id: int
    user_id: int
    phone: str
    plan: str
    plan_label: Optional[str] = None
    order_type: str = "membership"
    credit_amount: str = "0.00"
    amount: str
    pay_status: str
    trade_no: Optional[str]
    created_at: datetime
    paid_at: Optional[datetime]


class OrdersResponse(BaseModel):
    total: int
    items: List[OrderItem]


class GrantRequest(BaseModel):
    phone: str
    plan: str
    duration_days: Optional[int] = None


class GrantResponse(BaseModel):
    user_id: int
    phone: str
    plan: str
    membership_type: str
    expire_at: datetime
    message: str


class RevokeRequest(BaseModel):
    phone: str


class RevokeResponse(BaseModel):
    message: str


class ApiConfigUpdateRequest(BaseModel):
    settings: dict[str, Optional[str]] = {}
    clear_keys: list[str] = []


class PlanConfigUpdateRequest(BaseModel):
    plans: list[dict[str, Any]]
    credit_packages: Optional[list[dict[str, Any]]] = None
    credit_billing: Optional[dict[str, Any]] = None


class ChatModelItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    enabled: bool = True
    supports_vision: bool = False
    allowed_plans: list[str] = []


class ModelConfigUpdateRequest(BaseModel):
    default_model: str
    models: list[ChatModelItem]
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    clear_keys: list[str] = []


class ModelConfigTestRequest(BaseModel):
    model: str
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
def admin_login(body: LoginRequest, db: Session = Depends(get_db)):
    """Verify admin token. Returns the same token for frontend storage."""
    if not _token_is_admin(body.token, db):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return LoginResponse(token=body.token, message="Login successful")


@router.get("/stats", response_model=StatsResponse)
def get_stats(_: None = Depends(require_admin), db: Session = Depends(get_db)):
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = db.query(func.count(User.id)).scalar() or 0

    paid_members = (
        db.query(func.count(Membership.id))
        .filter(
            Membership.status == "active",
            (Membership.expire_at == None) | (Membership.expire_at > now),
        )
        .scalar() or 0
    )

    today_new_users = (
        db.query(func.count(User.id))
        .filter(User.created_at >= today_start)
        .scalar() or 0
    )

    today_conversations = (
        db.query(func.count(UsageLog.id))
        .filter(UsageLog.created_at >= today_start)
        .scalar() or 0
    )

    return StatsResponse(
        total_users=total_users,
        paid_members=paid_members,
        today_new_users=today_new_users,
        today_conversations=today_conversations,
    )


ADMIN_STATS_TZ = timezone(timedelta(hours=int(os.getenv("ADMIN_STATS_UTC_OFFSET_HOURS", "8"))))
ADMIN_STATS_TZ_LABEL = os.getenv("ADMIN_STATS_TZ_LABEL", "UTC+08:00")
MAX_USAGE_STATS_DAYS = 90


def _local_day_utc_bounds(day: date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, time.min, tzinfo=ADMIN_STATS_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _usage_day(created_at: datetime) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(ADMIN_STATS_TZ).date().isoformat()


@router.get("/usage-stats", response_model=UsageStatsResponse)
def get_usage_stats(
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD, local day in admin timezone"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD, local day in admin timezone"),
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    today = datetime.now(ADMIN_STATS_TZ).date()
    end_day = end_date or today
    start_day = start_date or (end_day - timedelta(days=6))
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    if (end_day - start_day).days + 1 > MAX_USAGE_STATS_DAYS:
        raise HTTPException(status_code=400, detail=f"最多只能查询 {MAX_USAGE_STATS_DAYS} 天")

    start_utc, _ = _local_day_utc_bounds(start_day)
    _, end_utc = _local_day_utc_bounds(end_day)

    rows = (
        db.query(
            UsageLog.user_id,
            User.phone,
            User.nickname,
            UsageLog.model,
            UsageLog.message_count,
            UsageLog.created_at,
        )
        .join(User, UsageLog.user_id == User.id)
        .filter(UsageLog.created_at >= start_utc, UsageLog.created_at < end_utc)
        .order_by(UsageLog.created_at.asc())
        .all()
    )

    day_count = (end_day - start_day).days + 1
    day_keys = [(start_day + timedelta(days=i)).isoformat() for i in range(day_count)]
    daily = {
        key: {
            "active_user_ids": set(),
            "usage_count": 0,
            "message_count": 0,
            "models": defaultdict(int),
            "users": {},
        }
        for key in day_keys
    }
    period_user_ids = set()

    for row in rows:
        day_key = _usage_day(row.created_at)
        if day_key not in daily:
            continue
        message_count = int(row.message_count or 1)
        bucket = daily[day_key]
        bucket["active_user_ids"].add(int(row.user_id))
        bucket["usage_count"] += 1
        bucket["message_count"] += message_count
        bucket["models"][row.model or "unknown"] += 1
        period_user_ids.add(int(row.user_id))

        user_bucket = bucket["users"].setdefault(int(row.user_id), {
            "user_id": int(row.user_id),
            "phone": row.phone,
            "nickname": row.nickname,
            "usage_count": 0,
            "message_count": 0,
            "last_active_at": row.created_at,
        })
        user_bucket["usage_count"] += 1
        user_bucket["message_count"] += message_count
        if row.created_at > user_bucket["last_active_at"]:
            user_bucket["last_active_at"] = row.created_at

    day_items: list[DailyUsageStat] = []
    total_usage_count = 0
    total_message_count = 0
    total_daily_active = 0
    for key in day_keys:
        bucket = daily[key]
        total_usage_count += bucket["usage_count"]
        total_message_count += bucket["message_count"]
        total_daily_active += len(bucket["active_user_ids"])
        top_users = sorted(
            bucket["users"].values(),
            key=lambda item: (
                item["usage_count"],
                item["message_count"],
                item["last_active_at"],
            ),
            reverse=True,
        )[:10]
        models = [
            UsageModelStat(model=model, usage_count=count)
            for model, count in sorted(bucket["models"].items(), key=lambda item: item[1], reverse=True)
        ]
        day_items.append(DailyUsageStat(
            day=key,
            active_users=len(bucket["active_user_ids"]),
            usage_count=bucket["usage_count"],
            message_count=bucket["message_count"],
            models=models,
            top_users=[UsageTopUser(**item) for item in top_users],
        ))

    return UsageStatsResponse(
        timezone=ADMIN_STATS_TZ_LABEL,
        start_date=start_day.isoformat(),
        end_date=end_day.isoformat(),
        totals=UsageTotals(
            active_users=len(period_user_ids),
            usage_count=total_usage_count,
            message_count=total_message_count,
            avg_daily_active_users=round(total_daily_active / day_count, 1) if day_count else 0,
        ),
        days=day_items,
    )


@router.get("/api-config")
def get_api_config(_: None = Depends(require_admin), db: Session = Depends(get_db)):
    return {"items": get_api_config_items(db)}


@router.put("/api-config")
def update_api_config(
    body: ApiConfigUpdateRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    changed = save_api_config(db, body.settings, body.clear_keys)
    return {"ok": True, "changed": changed, "items": get_api_config_items(db)}


@router.get("/plan-config")
def get_admin_plan_config(_: None = Depends(require_admin), db: Session = Depends(get_db)):
    return {
        **get_plan_config(db),
        "features": get_feature_definitions(),
        "credit_packages": get_credit_package_config(db)["packages"],
        "credit_billing": get_credit_billing_config(db),
    }


@router.put("/plan-config")
def update_admin_plan_config(
    body: PlanConfigUpdateRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model_ids = {
        str(model.get("id") or "").strip()
        for model in get_chat_model_config(db).get("models", [])
        if str(model.get("id") or "").strip() and model.get("enabled")
    }
    try:
        config = save_plan_config(db, body.model_dump(), valid_model_ids=model_ids)
        if body.credit_packages is not None:
            save_credit_package_config(db, {"packages": body.credit_packages})
        if body.credit_billing is not None:
            save_credit_billing_config(db, body.credit_billing, valid_model_ids=model_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        **config,
        "features": get_feature_definitions(),
        "credit_packages": get_credit_package_config(db)["packages"],
        "credit_billing": get_credit_billing_config(db),
    }


@router.get("/practice-content-options")
def get_practice_content_options(_: None = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(PracticeCourse).order_by(
        PracticeCourse.content_kind.asc(),
        PracticeCourse.sort_order.asc(),
        PracticeCourse.id.asc(),
    ).all()

    def item(course: PracticeCourse) -> dict:
        return {
            "id": int(course.id),
            "slug": course.slug,
            "title": course.title,
            "content_kind": course.content_kind or "course",
            "category": course.category,
            "is_published": bool(course.is_published),
            "lesson_count": int(course.lesson_count or 0),
        }

    return {
        "courses": [item(c) for c in rows if (c.content_kind or "course") == "course"],
        "columns": [item(c) for c in rows if c.content_kind == "column"],
    }


def _model_config_response(db: Session) -> dict:
    config = get_chat_model_config(db)
    return {
        "provider": get_chat_provider_status(db),
        "default_model": config["default_model"],
        "models": config["models"],
        "plans": [plan["id"] for plan in get_plan_config(db)["plans"]],
    }


@router.get("/model-config")
def get_model_config(_: None = Depends(require_admin), db: Session = Depends(get_db)):
    return _model_config_response(db)


@router.put("/model-config")
def update_model_config(
    body: ModelConfigUpdateRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    models = [m.model_dump() for m in body.models]
    if not models:
        raise HTTPException(status_code=400, detail="至少需要保留一个模型")

    model_ids = {str(m.get("id") or "").strip() for m in models}
    enabled_model_ids = {
        str(m.get("id") or "").strip()
        for m in models
        if str(m.get("id") or "").strip() and m.get("enabled")
    }
    if body.default_model not in model_ids:
        raise HTTPException(status_code=400, detail="默认模型必须存在于模型列表中")
    plan_config_exists = db.execute(
        text("SELECT 1 FROM app_settings WHERE key = 'PLAN_CONFIG'")
    ).fetchone()
    if plan_config_exists:
        try:
            normalize_plan_config(
                get_plan_config(db),
                db=db,
                valid_model_ids=enabled_model_ids,
                strict=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"模型保存后套餐配置会失效：{exc}")

    settings: dict[str, Optional[str]] = {}
    clear_keys = [k for k in body.clear_keys if k in {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"}]
    if body.anthropic_api_key is not None and body.anthropic_api_key.strip():
        settings["ANTHROPIC_API_KEY"] = body.anthropic_api_key.strip()
    if body.anthropic_base_url is not None:
        settings["ANTHROPIC_BASE_URL"] = body.anthropic_base_url.strip()
    if settings or clear_keys:
        save_api_config(db, settings, clear_keys)

    save_chat_model_config(db, {
        "default_model": body.default_model,
        "models": models,
    })
    return {"ok": True, **_model_config_response(db)}


@router.post("/model-config/test")
async def test_model_config(
    body: ModelConfigTestRequest,
    _: None = Depends(require_admin),
):
    model = (body.model or "").strip()
    api_key = (body.anthropic_api_key or get_app_setting("ANTHROPIC_API_KEY", "") or "").strip()
    base_url = (body.anthropic_base_url if body.anthropic_base_url is not None else get_app_setting("ANTHROPIC_BASE_URL", "") or "").strip()
    if not model:
        return {"ok": False, "message": "请先选择要测试的模型"}
    if not api_key:
        return {"ok": False, "message": "ANTHROPIC_API_KEY 未配置，无法测试对话模型"}

    try:
        kwargs = {
            "api_key": api_key,
            "timeout": httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0),
        }
        if base_url:
            kwargs["base_url"] = base_url
        client = anthropic.AsyncAnthropic(**kwargs)
        resp = await client.messages.create(
            model=model,
            max_tokens=32,
            system="你是一个用于后台连通性测试的助手。只用中文简短回答。",
            messages=[{"role": "user", "content": "请回复：模型连接正常"}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return {"ok": True, "message": "模型连接成功", "sample": text[:120]}
    except Exception as exc:
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


@router.get("/users", response_model=UsersResponse)
def list_users(
    phone: Optional[str] = Query(None, description="Filter by phone (partial match)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    now = datetime.utcnow()
    q = db.query(User)
    if phone:
        q = q.filter(User.phone.contains(phone))

    total = q.count()
    users = q.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for user in users:
        active_m = (
            db.query(Membership)
            .filter(
                Membership.user_id == user.id,
                Membership.status == "active",
                (Membership.expire_at == None) | (Membership.expire_at > now),
            )
            .order_by(Membership.expire_at.desc().nullslast())
            .first()
        )
        credit = db.query(Credit).filter(Credit.user_id == user.id).first()
        items.append(UserItem(
            id=user.id,
            phone=user.phone,
            nickname=user.nickname,
            created_at=user.created_at,
            is_active=user.is_active,
            credit_balance=str(credit.balance if credit else "0.00"),
            membership=MembershipInfo(
                plan_type=active_m.plan_type if active_m else None,
                expire_at=active_m.expire_at if active_m else None,
                status=active_m.status if active_m else None,
            ) if active_m else None,
        ))

    return UsersResponse(total=total, items=items)


@router.get("/orders", response_model=OrdersResponse)
def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total = db.query(func.count(Order.id)).scalar() or 0
    orders = (
        db.query(Order, User.phone)
        .join(User, Order.user_id == User.id)
        .order_by(Order.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [
        OrderItem(
            id=o.id,
            user_id=o.user_id,
            phone=phone,
            plan=o.plan,
            plan_label=o.plan_label,
            order_type=getattr(o, "order_type", "membership"),
            credit_amount=str(getattr(o, "credit_amount", 0) or 0),
            amount=str(o.amount),
            pay_status=o.pay_status,
            trade_no=o.trade_no,
            created_at=o.created_at,
            paid_at=o.paid_at,
        )
        for o, phone in orders
    ]

    return OrdersResponse(total=total, items=items)


@router.post("/grant", response_model=GrantResponse)
def grant_membership(
    body: GrantRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    plan = body.plan.strip().lower()
    grantable = {item["id"]: item for item in get_grantable_plans(db)}
    plan_cfg = grantable.get(plan)
    if plan_cfg is None:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Choose from: {sorted(grantable)}")

    user = db.query(User).filter(User.phone == body.phone).first()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {body.phone} not found")

    days = body.duration_days or int(plan_cfg["duration_days"])
    membership_type = plan
    now = datetime.utcnow()

    existing = db.query(Membership).filter(
        Membership.user_id == user.id, Membership.status == "active"
    ).first()

    if existing:
        base = existing.expire_at if existing.expire_at and existing.expire_at > now else now
        expire_at = base + timedelta(days=days)
        existing.plan_type = membership_type
        existing.expire_at = expire_at
    else:
        expire_at = now + timedelta(days=days)
        db.add(Membership(
            user_id=user.id, plan_type=membership_type,
            start_at=now, expire_at=expire_at, status="active",
        ))

    db.commit()
    return GrantResponse(
        user_id=user.id, phone=user.phone, plan=plan,
        membership_type=membership_type, expire_at=expire_at,
        message=f"Successfully granted {plan_cfg['name']} ({days} days) to {body.phone}",
    )


@router.post("/revoke", response_model=RevokeResponse)
def revoke_membership(
    body: RevokeRequest,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.phone == body.phone).first()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {body.phone} not found")

    memberships = db.query(Membership).filter(
        Membership.user_id == user.id, Membership.status == "active"
    ).all()

    if not memberships:
        raise HTTPException(status_code=404, detail="No active membership found")

    for m in memberships:
        m.status = "cancelled"
    db.commit()

    return RevokeResponse(message=f"Membership revoked for {body.phone}")
