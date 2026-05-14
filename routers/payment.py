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
from decimal import Decimal
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from credit_config import get_credit_billing_config, get_purchasable_credit_packages
from credit_service import grant_credits
from models import Membership, Order, User
from permissions import ensure_plan_period_credits_for_user_id
from plan_config import get_plan_definition, get_purchasable_plans

load_dotenv()

router = APIRouter(prefix="/pay", tags=["payment"])

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

def _activate_membership(order: Order, db: Session) -> None:
    plan_cfg = get_plan_definition(order.plan, db)
    membership_type = order.plan
    duration_days = int(order.plan_duration_days or plan_cfg.get("duration_days") or 0)
    if duration_days <= 0:
        raise RuntimeError(f"Invalid duration for plan {order.plan}")
    now = datetime.utcnow()

    existing = (
        db.query(Membership)
        .filter(Membership.user_id == order.user_id, Membership.status == "active")
        .first()
    )
    if existing:
        base = existing.expire_at if existing.expire_at and existing.expire_at > now else now
        expire_at = base + timedelta(days=duration_days)
        existing.plan_type = membership_type
        existing.expire_at = expire_at
    else:
        expire_at = now + timedelta(days=duration_days)
        db.add(Membership(
            user_id=order.user_id,
            plan_type=membership_type,
            start_at=now,
            expire_at=expire_at,
            status="active",
        ))
    db.flush()
    ensure_plan_period_credits_for_user_id(
        int(order.user_id),
        db,
        membership_type,
    )


def _activate_credit_order(order: Order, db: Session) -> None:
    credit_amount = Decimal(str(order.credit_amount or "0"))
    if credit_amount <= 0:
        raise RuntimeError(f"Invalid credit amount for order {order.id}")
    grant_credits(
        db,
        user_id=int(order.user_id),
        amount=credit_amount,
        source_type="order",
        source_id=str(order.id),
        description=order.plan_label or "积分购买",
    )


def _mark_paid_and_fulfill(order: Order, db: Session) -> None:
    """Idempotent: mark order paid and fulfill membership or credit purchase."""
    if order.pay_status == "paid":
        if getattr(order, "order_type", "membership") == "credits":
            _activate_credit_order(order, db)
            db.commit()
        elif getattr(order, "order_type", "membership") == "membership":
            ensure_plan_period_credits_for_user_id(int(order.user_id), db, str(order.plan))
        return
    order.pay_status = "paid"
    order.paid_at = datetime.utcnow()
    db.flush()
    if getattr(order, "order_type", "membership") == "credits":
        _activate_credit_order(order, db)
    else:
        _activate_membership(order, db)
    db.commit()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    plan: Optional[str] = None
    product_type: str = "membership"
    package_id: Optional[str] = None


class CreateOrderResponse(BaseModel):
    order_id: int
    trade_no: str
    amount: str
    pay_url: str
    is_mock: bool
    order_type: str = "membership"
    credit_amount: str = "0.00"


class PaymentPlanItem(BaseModel):
    id: str
    name: str
    description: str
    benefits: list[str] = []
    amount: str
    duration_days: int
    period: str
    quota: int
    allowed_models: list[str]
    free_models: list[str]
    enabled_features: list[str]
    feature_quotas: dict[str, int] = {}
    practice_access: bool
    practice_publish: bool


class PaymentCreditPackageItem(BaseModel):
    id: str
    name: str
    description: str = ""
    amount: str
    credits: int


class PaymentPlansResponse(BaseModel):
    payments_enabled: bool
    free_plan: Optional[PaymentPlanItem] = None
    plans: list[PaymentPlanItem]
    credit_packages: list[PaymentCreditPackageItem] = []
    credit_billing: dict = {}


class OrderStatusResponse(BaseModel):
    order_id: int
    trade_no: str
    plan: str
    amount: str
    plan_label: Optional[str] = None
    plan_duration_days: Optional[int] = None
    order_type: str = "membership"
    credit_amount: str = "0.00"
    pay_status: str
    paid_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/plans", response_model=PaymentPlansResponse)
def list_payment_plans(db: Session = Depends(get_db)):
    return PaymentPlansResponse(
        payments_enabled=_payments_enabled(),
        free_plan=PaymentPlanItem(**get_plan_definition("free", db)),
        plans=[PaymentPlanItem(**plan) for plan in get_purchasable_plans(db)],
        credit_packages=[
            PaymentCreditPackageItem(**package)
            for package in get_purchasable_credit_packages(db)
        ],
        credit_billing=get_credit_billing_config(db),
    )


@router.post("/create", response_model=CreateOrderResponse)
def create_order(
    body: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _payments_enabled():
        raise HTTPException(status_code=409, detail="当前套餐已售罄")

    product_type = (body.product_type or "membership").strip().lower()
    if product_type not in {"membership", "credits"}:
        raise HTTPException(status_code=400, detail="Invalid product_type")

    if product_type == "credits":
        package_id = (body.package_id or body.plan or "").strip().lower()
        packages = {item["id"]: item for item in get_purchasable_credit_packages(db)}
        product_cfg = packages.get(package_id)
        if product_cfg is None:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid credit package. Choose from: {list(packages.keys())}",
            )
        product_id = product_cfg["id"]
        amount = product_cfg["amount"]
        label = product_cfg["name"]
        duration_days = None
        credit_amount = Decimal(str(product_cfg["credits"]))
        subject = f"阿川工作台 - {label}"
    else:
        plan = (body.plan or "").strip().lower()
        purchasable = {item["id"]: item for item in get_purchasable_plans(db)}
        product_cfg = purchasable.get(plan)
        if product_cfg is None:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid plan. Choose from: {list(purchasable.keys())}",
            )
        product_id = product_cfg["id"]
        amount = product_cfg["amount"]
        label = product_cfg["name"]
        duration_days = int(product_cfg["duration_days"])
        credit_amount = Decimal(str(product_cfg.get("quota") or "0"))
        subject = f"阿川工作台 - {label}"

    if not _alipay_configured():
        raise HTTPException(status_code=503, detail="支付暂未开放")

    trade_no = f"AI{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"

    order = Order(
        user_id=current_user.id,
        plan=product_id,
        amount=Decimal(str(amount)),
        plan_label=label,
        plan_duration_days=duration_days,
        order_type=product_type,
        credit_amount=credit_amount,
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
            amount,
            subject,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Alipay error: {e}")

    return CreateOrderResponse(
        order_id=order.id,
        trade_no=trade_no,
        amount=amount,
        pay_url=pay_url,
        is_mock=False,
        order_type=product_type,
        credit_amount=str(credit_amount),
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

    _mark_paid_and_fulfill(order, db)
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
            _mark_paid_and_fulfill(order, db)
            db.refresh(order)

    return OrderStatusResponse(
        order_id=order.id,
        trade_no=order.trade_no,
        plan=order.plan,
        amount=str(order.amount),
        plan_label=order.plan_label,
        plan_duration_days=order.plan_duration_days,
        order_type=getattr(order, "order_type", "membership"),
        credit_amount=str(getattr(order, "credit_amount", 0) or 0),
        pay_status=order.pay_status,
        paid_at=order.paid_at,
    )
