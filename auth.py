import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from database import get_db
from models import User

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))  # 7 days

bearer_scheme = HTTPBearer()

# 🔍 启动时打印一次，确认配置
print(f"[AUTH] SECRET_KEY={SECRET_KEY[:20]}... (len={len(SECRET_KEY)})")
print(f"[AUTH] ALGORITHM={ALGORITHM}")
print(f"[AUTH] TOKEN_EXPIRE_MINUTES={ACCESS_TOKEN_EXPIRE_MINUTES}")


def _normalize_phone(phone: Optional[str]) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _split_phone_list(raw: Optional[str]) -> list[str]:
    phones: list[str] = []
    for item in (raw or "").replace("，", ",").split(","):
        phone = _normalize_phone(item)
        if phone:
            phones.append(phone)
    return phones


def get_default_admin_phones() -> set[str]:
    """Phones that should be promoted to admin automatically on login."""
    phones: list[str] = []
    phones.extend(_split_phone_list(os.getenv("DEFAULT_ADMIN_PHONES")))
    phones.extend(_split_phone_list(os.getenv("FIRST_ADMIN_PHONE")))
    if not phones:
        # Backward-compatible default for the current deployment.
        phones.append("13260018535")
    return set(phones)


def is_default_admin_phone(phone: Optional[str]) -> bool:
    return _normalize_phone(phone) in get_default_admin_phones()


def ensure_default_admin(user: User, db: Session) -> User:
    if user and is_default_admin_phone(user.phone) and not getattr(user, "is_admin", False):
        user.is_admin = True
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"[AUTH] ✓ 默认管理员已自动授权 phone={user.phone}")
    return user


def create_access_token(user_id: int, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {"sub": str(user_id), "exp": expire}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    print(f"[AUTH] 签发 token: user_id={user_id} expire={expire} token_prefix={token[:30]}...")
    return token


def decode_access_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            print(f"[AUTH] ❌ token 解码后 sub 字段为空")
            return None
        return int(user_id)
    except JWTError as e:
        print(f"[AUTH] ❌ JWT 解码失败: {type(e).__name__}: {e}")
        print(f"[AUTH]    收到的 token 前 50 位: {token[:50]}")
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    print(f"[AUTH] 收到请求 token 前 30 位: {token[:30]}...")

    user_id = decode_access_token(token)
    if user_id is None:
        print(f"[AUTH] ❌ 401: token 解码失败")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    print(f"[AUTH] ✓ token 解码成功 user_id={user_id}")

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if user is None:
        # 再查一次不带 is_active 条件，看是用户不存在还是被禁用
        user_check = db.query(User).filter(User.id == user_id).first()
        if user_check is None:
            print(f"[AUTH] ❌ 401: 数据库里找不到 user_id={user_id}")
        else:
            print(f"[AUTH] ❌ 401: user_id={user_id} 存在但 is_active={user_check.is_active}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = ensure_default_admin(user, db)
    print(f"[AUTH] ✓ 鉴权成功 user_id={user.id} phone={user.phone}")
    return user
