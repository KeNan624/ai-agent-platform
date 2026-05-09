from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import create_access_token, ensure_default_admin, get_current_user
from database import get_db
from models import User
from sms_service import (
    SmsConfigError,
    SmsProviderError,
    SmsRateLimitError,
    SmsVerificationError,
    send_sms_code,
    verify_sms_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SendCodeRequest(BaseModel):
    phone: str


class SendCodeResponse(BaseModel):
    message: str
    expire_seconds: int
    resend_seconds: int
    code: Optional[str] = None


class LoginRequest(BaseModel):
    phone: str
    code: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    is_new_user: bool


class UserInfo(BaseModel):
    id: int
    phone: str
    nickname: Optional[str]
    avatar: Optional[str]
    created_at: datetime
    is_active: bool
    is_admin: bool = False

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/send-code", response_model=SendCodeResponse)
def send_code(body: SendCodeRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else None
    try:
        result = send_sms_code(db, body.phone, purpose="login", client_ip=client_ip)
    except SmsRateLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except SmsVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SmsConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except SmsProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return SendCodeResponse(
        message="验证码已发送",
        expire_seconds=result.expire_seconds,
        resend_seconds=result.resend_seconds,
        code=result.dev_code,
    )


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    try:
        phone = verify_sms_code(db, body.phone, body.code, purpose="login")
    except SmsVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    user = db.query(User).filter(User.phone == phone).first()
    is_new_user = user is None
    if is_new_user:
        user = User(phone=phone, created_at=datetime.utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)
    user = ensure_default_admin(user, db)

    token = create_access_token(user.id)
    return LoginResponse(
        access_token=token,
        user_id=user.id,
        is_new_user=is_new_user,
    )


@router.get("/me", response_model=UserInfo)
def me(current_user: User = Depends(get_current_user)):
    return current_user
