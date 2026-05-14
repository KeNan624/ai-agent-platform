from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from models import User
from database import get_db
from permissions import get_quota_status
from sqlalchemy.orm import Session

router = APIRouter(prefix="/member", tags=["member"])


class MemberInfoResponse(BaseModel):
    phone: str
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    plan_type: str
    plan_name: str
    expire_at: Optional[datetime]
    period: str                  # "day" or "month"
    quota: int                   # total allowed calls per period
    used: int                    # calls used in current period
    remaining: int               # calls left in current period
    allowed_models: List[str]
    free_models: List[str] = []
    enabled_features: List[str] = []
    feature_quotas: dict[str, int] = {}
    feature_usage: dict[str, int] = {}
    practice_access: bool = False
    practice_publish: bool = False


class MemberInfoUpdate(BaseModel):
    nickname: Optional[str] = None


@router.get("/info", response_model=MemberInfoResponse)
def member_info(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    status = get_quota_status(current_user, db)
    return MemberInfoResponse(
        phone=current_user.phone,
        nickname=current_user.nickname,
        avatar=current_user.avatar,
        **status,
    )


@router.patch("/info", response_model=MemberInfoResponse)
def update_member_info(
    body: MemberInfoUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.nickname is not None:
        nickname = body.nickname.strip()
        current_user.nickname = nickname[:64] or None
        db.add(current_user)
        db.commit()
        db.refresh(current_user)

    status = get_quota_status(current_user, db)
    return MemberInfoResponse(
        phone=current_user.phone,
        nickname=current_user.nickname,
        avatar=current_user.avatar,
        **status,
    )
