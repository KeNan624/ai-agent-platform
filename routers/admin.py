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
from datetime import datetime, timedelta
from typing import List, Optional

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app_config import get_api_config_items, get_app_setting, save_api_config
from chat_model_config import (
    get_chat_model_config,
    get_chat_provider_status,
    save_chat_model_config,
)
from database import get_db
from models import Membership, Order, UsageLog, User

load_dotenv()

router = APIRouter(prefix="/admin", tags=["admin"])

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")

bearer_scheme = HTTPBearer()

VALID_PLANS = {"monthly", "quarterly", "yearly"}

PLAN_DURATION_DAYS: dict[str, int] = {
    "monthly":   30,
    "quarterly": 90,
    "yearly":    365,
}

PLAN_TO_MEMBERSHIP: dict[str, str] = {
    "monthly":   "monthly",
    "quarterly": "monthly",
    "yearly":    "yearly",
}


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


def _model_config_response(db: Session) -> dict:
    config = get_chat_model_config(db)
    return {
        "provider": get_chat_provider_status(db),
        "default_model": config["default_model"],
        "models": config["models"],
        "plans": ["free", "monthly", "yearly"],
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
    if body.default_model not in model_ids:
        raise HTTPException(status_code=400, detail="默认模型必须存在于模型列表中")

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
        items.append(UserItem(
            id=user.id,
            phone=user.phone,
            nickname=user.nickname,
            created_at=user.created_at,
            is_active=user.is_active,
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
    plan = body.plan.lower()
    if plan not in VALID_PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Choose from: {sorted(VALID_PLANS)}")

    user = db.query(User).filter(User.phone == body.phone).first()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {body.phone} not found")

    days = body.duration_days or PLAN_DURATION_DAYS[plan]
    membership_type = PLAN_TO_MEMBERSHIP[plan]
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
        message=f"Successfully granted {plan} ({days} days) to {body.phone}",
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
