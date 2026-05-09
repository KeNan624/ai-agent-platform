"""
Payment router — Alipay integration (alipay-sdk-python new-style API).

Endpoints
---------
POST /pay/create            Create order + return Alipay QR pay URL
POST /pay/callback          Alipay async notify (verify sig, activate membership)
GET  /pay/status/{order_id} Poll order pay status (also actively queries Alipay
                            when in polling-only mode, i.e. NOTIFY_URL is empty)
GET  /pay/mock              Disabled legacy endpoint

Key configuration
-----------------
Alipay keys can come from either:
  a) Files (preferred, more secure):
     - ALIPAY_PRIVATE_KEY_FILE=/absolute/path/app_private_key_pkcs8.pem
     - ALIPAY_PUBLIC_KEY_FILE=/absolute/path/alipay_public_key.pem
  b) Env vars (legacy, less secure):
     - ALIPAY_PRIVATE_KEY=... (with \\n for newlines)
     - ALIPAY_PUBLIC_KEY=...

Files win if both are set.
"""

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import Membership, Order, User

load_dotenv()

router = APIRouter(prefix="/pay", tags=["payment"])

# ---------------------------------------------------------------------------
# Plan catalogue
# ---------------------------------------------------------------------------

PLANS: dict[str, dict] = {
    "monthly":   {"amount": "99.00",  "duration_days": 30,  "label": "月度会员"},
    "quarterly": {"amount": "249.00", "duration_days": 90,  "label": "季度会员"},
    "yearly":    {"amount": "799.00", "duration_days": 365, "label": "年度会员"},
}

PLAN_TO_MEMBERSHIP: dict[str, str] = {
    "monthly":   "monthly",
    "quarterly": "monthly",
    "yearly":    "yearly",
}


# ---------------------------------------------------------------------------
# Key loading helpers (file-based preferred, env-based fallback)
# ---------------------------------------------------------------------------

def _load_key_content(file_env: str, inline_env: str) -> str:
    """Return key content. File path takes precedence over inline env var.

    Strips PEM header/footer lines so we hand the naked base64 body to the
    Alipay SDK, which is what its config expects.
    """
    file_path = os.getenv(file_env, "").strip()
    if file_path:
        expanded = os.path.expanduser(file_path)
        try:
            raw = Path(expanded).read_text()
        except FileNotFoundError:
            raise RuntimeError(
                f"{file_env} points to {expanded} but the file does not exist"
            )
        lines = [ln for ln in raw.splitlines() if "BEGIN" not in ln and "END" not in ln]
        return "".join(lines).strip()

    inline = os.getenv(inline_env, "").replace("\\n", "\n")
    lines = [ln for ln in inline.splitlines() if "BEGIN" not in ln and "END" not in ln]
    return "".join(lines).strip()


def _alipay_configured() -> bool:
    """True if we have enough config to actually talk to Alipay."""
    if not os.getenv("ALIPAY_APP_ID"):
        return False
    has_priv = bool(os.getenv("ALIPAY_PRIVATE_KEY_FILE") or os.getenv("ALIPAY_PRIVATE_KEY"))
    return has_priv


def _payments_enabled() -> bool:
    """Payments are closed by default; set PAYMENTS_ENABLED=true to reopen sales."""
    return os.getenv("PAYMENTS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Alipay client
# ---------------------------------------------------------------------------

def _make_client():
    """Build and return a DefaultAlipayClient (new-style SDK)."""
    from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
    from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

    cfg = AlipayClientConfig()
    cfg.server_url = (
        "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
        if os.getenv("ALIPAY_DEBUG", "false").lower() == "true"
        else "https://openapi.alipay.com/gateway.do"
    )
    cfg.app_id             = os.getenv("ALIPAY_APP_ID", "")
    cfg.app_private_key    = _load_key_content("ALIPAY_PRIVATE_KEY_FILE", "ALIPAY_PRIVATE_KEY")
    cfg.alipay_public_key  = _load_key_content("ALIPAY_PUBLIC_KEY_FILE", "ALIPAY_PUBLIC_KEY")
    cfg.charset            = "utf-8"
    cfg.sign_type          = "RSA2"
    return DefaultAlipayClient(alipay_client_config=cfg)


def _build_qr_url(trade_no: str, amount: str, subject: str) -> str:
    """Call alipay.trade.precreate and return the QR code URL."""
    from alipay.aop.api.domain.AlipayTradePrecreateModel import AlipayTradePrecreateModel
    from alipay.aop.api.request.AlipayTradePrecreateRequest import AlipayTradePrecreateRequest

    client = _make_client()
    model = AlipayTradePrecreateModel()
    model.out_trade_no = trade_no
    model.total_amount = amount
    model.subject = subject

    req = AlipayTradePrecreateRequest(biz_model=model)
    notify_url = os.getenv("ALIPAY_NOTIFY_URL", "").strip()
    if notify_url:
        req.notify_url = notify_url

    resp_str = client.execute(req)
    resp = json.loads(resp_str)
    body = resp.get("alipay_trade_precreate_response", {})
    if body.get("code") != "10000":
        raise RuntimeError(f"Alipay error: {body.get('sub_msg', body.get('msg'))}")
    return body["qr_code"]


def _query_trade_status(out_trade_no: str) -> Optional[str]:
    """Actively ask Alipay whether this order has been paid.

    Returns Alipay's trade_status string (e.g. 'TRADE_SUCCESS',
    'TRADE_FINISHED', 'WAIT_BUYER_PAY') or None if the query itself fails
    or the trade doesn't exist yet. Used in polling-only mode when we have
    no public notify_url.
    """
    from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
    from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest

    try:
        client = _make_client()
        model = AlipayTradeQueryModel()
        model.out_trade_no = out_trade_no

        req = AlipayTradeQueryRequest(biz_model=model)
        resp_str = client.execute(req)
        resp = json.loads(resp_str)
        body = resp.get("alipay_trade_query_response", {})
        # ACQ.TRADE_NOT_EXIST is normal before the user has scanned
        if body.get("code") != "10000":
            return None
        return body.get("trade_status")
    except Exception as e:
        print(f"[pay] trade.query failed for {out_trade_no}: {e}", flush=True)
        return None


def _verify_callback(params: dict, sign: str) -> bool:
    """Verify Alipay async notification signature."""
    from alipay.aop.api.util.SignatureUtils import get_sign_content, verify_with_rsa

    pub_key = _load_key_content("ALIPAY_PUBLIC_KEY_FILE", "ALIPAY_PUBLIC_KEY")
    content = get_sign_content(params)
    import base64
    try:
        raw_sign = base64.b64decode(sign)
        return verify_with_rsa(pub_key.encode(), content.encode("utf-8"), raw_sign)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Membership activation
# ---------------------------------------------------------------------------

def _activate_membership(user_id: int, plan: str, db: Session) -> None:
    plan_cfg = PLANS[plan]
    membership_type = PLAN_TO_MEMBERSHIP[plan]
    now = datetime.utcnow()

    existing = (
        db.query(Membership)
        .filter(Membership.user_id == user_id, Membership.status == "active")
        .first()
    )
    if existing:
        base = existing.expire_at if existing.expire_at and existing.expire_at > now else now
        expire_at = base + timedelta(days=plan_cfg["duration_days"])
        existing.plan_type = membership_type
        existing.expire_at = expire_at
    else:
        expire_at = now + timedelta(days=plan_cfg["duration_days"])
        db.add(Membership(
            user_id=user_id,
            plan_type=membership_type,
            start_at=now,
            expire_at=expire_at,
            status="active",
        ))
    db.commit()


def _mark_paid_and_activate(order: Order, db: Session) -> None:
    """Idempotent: mark order paid + activate membership exactly once."""
    if order.pay_status == "paid":
        return
    order.pay_status = "paid"
    order.paid_at = datetime.utcnow()
    db.commit()
    _activate_membership(order.user_id, order.plan, db)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    plan: str


class CreateOrderResponse(BaseModel):
    order_id: int
    trade_no: str
    amount: str
    pay_url: str
    is_mock: bool


class OrderStatusResponse(BaseModel):
    order_id: int
    trade_no: str
    plan: str
    amount: str
    pay_status: str
    paid_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/create", response_model=CreateOrderResponse)
def create_order(
    body: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _payments_enabled():
        raise HTTPException(status_code=409, detail="当前套餐已售罄")

    plan = body.plan.lower()
    if plan not in PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plan. Choose from: {list(PLANS.keys())}",
        )

    plan_cfg = PLANS[plan]
    if not _alipay_configured():
        raise HTTPException(status_code=503, detail="支付暂未开放")

    trade_no = f"AI{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"

    order = Order(
        user_id=current_user.id,
        plan=plan,
        amount=plan_cfg["amount"],
        pay_status="pending",
        trade_no=trade_no,
        created_at=datetime.utcnow(),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    try:
        pay_url = _build_qr_url(
            trade_no,
            plan_cfg["amount"],
            f"阿川工作台 - {plan_cfg['label']}",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Alipay error: {e}")

    return CreateOrderResponse(
        order_id=order.id,
        trade_no=trade_no,
        amount=plan_cfg["amount"],
        pay_url=pay_url,
        is_mock=False,
    )


@router.post("/callback")
async def alipay_callback(request: Request, db: Session = Depends(get_db)):
    """Alipay async notification — must be publicly accessible.

    In polling-only mode (NOTIFY_URL empty) this endpoint is never hit by
    Alipay, but we keep it so that switching to async mode later is just an
    env-var change.
    """
    form = await request.form()
    data = dict(form)
    sign = data.pop("sign", "")
    data.pop("sign_type", None)

    if _alipay_configured():
        if not _verify_callback(data, sign):
            return "fail"

    trade_status = data.get("trade_status", "")
    out_trade_no = data.get("out_trade_no", "")

    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return "success"

    order = db.query(Order).filter(Order.trade_no == out_trade_no).first()
    if order is None:
        return "fail"

    _mark_paid_and_activate(order, db)
    return "success"


@router.get("/mock")
def mock_pay(trade_no: str, db: Session = Depends(get_db)):
    """Legacy route kept for compatibility but no longer activates payments."""
    raise HTTPException(status_code=404, detail="Not found")


@router.get("/status/{order_id}", response_model=OrderStatusResponse)
def order_status(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current pay_status.

    POLLING-ONLY MODE: when Alipay is configured but NOTIFY_URL is empty,
    we actively query Alipay's trade.query API on every poll so we can
    detect payment without an async callback. This is what enables local
    testing with real Alipay without exposing a public URL.
    """
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.user_id == current_user.id,
    ).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    # If we're in real-Alipay mode but have no notify_url, proactively ask
    # Alipay whether the order has been paid yet, then update local state.
    if (
        order.pay_status == "pending"
        and _alipay_configured()
        and not os.getenv("ALIPAY_NOTIFY_URL", "").strip()
    ):
        trade_status = _query_trade_status(order.trade_no)
        if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            _mark_paid_and_activate(order, db)
            db.refresh(order)

    return OrderStatusResponse(
        order_id=order.id,
        trade_no=order.trade_no,
        plan=order.plan,
        amount=str(order.amount),
        pay_status=order.pay_status,
        paid_at=order.paid_at,
    )
