import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app_config import get_app_setting
from models import SmsVerificationCode


PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
CODE_RE = re.compile(r"^\d{6}$")

SMSBAO_ERROR_MESSAGES = {
    "30": "短信宝密码错误",
    "40": "短信宝账号不存在",
    "41": "短信宝余额不足",
    "43": "短信宝 IP 地址限制",
    "50": "短信内容含有敏感词",
    "51": "手机号不正确",
}


class SmsError(Exception):
    """Base class for expected SMS verification errors."""


class SmsConfigError(SmsError):
    pass


class SmsProviderError(SmsError):
    pass


class SmsRateLimitError(SmsError):
    pass


class SmsVerificationError(SmsError):
    pass


@dataclass
class SmsSendResult:
    phone: str
    expire_seconds: int
    resend_seconds: int
    dev_code: Optional[str] = None


def normalize_phone(raw_phone: str) -> str:
    phone = (raw_phone or "").strip().replace(" ", "").replace("-", "")
    if phone.startswith("+86"):
        phone = phone[3:]
    elif phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    if not PHONE_RE.match(phone):
        raise SmsVerificationError("请输入正确的 11 位手机号")
    return phone


def _get_int_setting(key: str, default: int, *, min_value: int = 1) -> int:
    raw = get_app_setting(key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(value, min_value)


def _get_bool_setting(key: str, default: bool = False) -> bool:
    raw = get_app_setting(key, "1" if default else "0")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _sms_hash_secret() -> bytes:
    secret = (
        get_app_setting("SMS_CODE_HASH_SECRET", "")
        or os.getenv("JWT_SECRET_KEY")
        or "change-me-in-production"
    )
    return str(secret).encode("utf-8")


def _hash_code(phone: str, code: str, purpose: str) -> str:
    payload = f"{phone}:{purpose}:{code}".encode("utf-8")
    return hmac.new(_sms_hash_secret(), payload, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _is_md5_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[a-fA-F0-9]{32}", value or ""))


def _smsbao_credential() -> str:
    api_key = (get_app_setting("SMSBAO_API_KEY", "") or "").strip()
    if api_key:
        return api_key

    password_md5 = (get_app_setting("SMSBAO_PASSWORD_MD5", "") or "").strip()
    if password_md5:
        return password_md5

    password = (get_app_setting("SMSBAO_PASSWORD", "") or "").strip()
    if not password:
        return ""
    if _is_md5_hex(password):
        return password
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _smsbao_configured() -> bool:
    return bool((get_app_setting("SMSBAO_USERNAME", "") or "").strip() and _smsbao_credential())


def _dev_mode_enabled() -> bool:
    raw = str(get_app_setting("SMS_DEV_MODE", "auto") or "auto").strip().lower()
    if raw in {"1", "true", "yes", "on", "dev"}:
        return True
    if raw in {"0", "false", "no", "off", "prod", "production"}:
        return False
    return not _smsbao_configured()


def _render_sms_content(code: str, ttl_seconds: int) -> str:
    template = (
        get_app_setting(
            "SMS_CODE_TEMPLATE",
            "【阿川AI】您的验证码是{code}，{minutes}分钟内有效。如非本人操作，请忽略。",
        )
        or ""
    ).strip()
    if not template:
        raise SmsConfigError("短信验证码模板未配置")
    minutes = max(1, ttl_seconds // 60)
    return template.format(code=code, minutes=minutes, expire_minutes=minutes)


def _send_by_smsbao(phone: str, code: str, ttl_seconds: int) -> str:
    username = (get_app_setting("SMSBAO_USERNAME", "") or "").strip()
    credential = _smsbao_credential()
    if not username or not credential:
        raise SmsConfigError("短信宝账号或 ApiKey 未配置")

    endpoint = (
        get_app_setting("SMSBAO_ENDPOINT", "https://api.smsbao.com/sms")
        or "https://api.smsbao.com/sms"
    ).strip()
    goods_id = (get_app_setting("SMSBAO_GOODS_ID", "") or "").strip()
    timeout = _get_int_setting("SMS_REQUEST_TIMEOUT_SECONDS", 10, min_value=1)
    params = {
        "u": username,
        "p": credential,
        "m": phone,
        "c": _render_sms_content(code, ttl_seconds),
    }
    if goods_id:
        params["g"] = goods_id

    try:
        response = httpx.get(endpoint, params=params, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SmsProviderError(f"短信宝请求失败：{exc}") from exc

    result = response.text.strip()
    if result != "0":
        message = SMSBAO_ERROR_MESSAGES.get(result, f"短信宝返回异常：{result or '空响应'}")
        raise SmsProviderError(message)
    return result


def _check_send_rate_limit(db: Session, phone: str, purpose: str, client_ip: Optional[str]) -> None:
    now = datetime.utcnow()
    resend_seconds = _get_int_setting("SMS_RESEND_INTERVAL_SECONDS", 60, min_value=1)
    latest = (
        db.query(SmsVerificationCode)
        .filter(
            SmsVerificationCode.phone == phone,
            SmsVerificationCode.purpose == purpose,
            SmsVerificationCode.status == "sent",
        )
        .order_by(SmsVerificationCode.created_at.desc())
        .first()
    )
    if latest and (now - latest.created_at).total_seconds() < resend_seconds:
        wait = resend_seconds - int((now - latest.created_at).total_seconds())
        raise SmsRateLimitError(f"验证码发送太频繁，请 {max(1, wait)} 秒后再试")

    daily_limit = _get_int_setting("SMS_DAILY_LIMIT_PER_PHONE", 10, min_value=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count = (
        db.query(func.count(SmsVerificationCode.id))
        .filter(
            SmsVerificationCode.phone == phone,
            SmsVerificationCode.purpose == purpose,
            SmsVerificationCode.status == "sent",
            SmsVerificationCode.created_at >= day_start,
        )
        .scalar()
        or 0
    )
    if daily_count >= daily_limit:
        raise SmsRateLimitError("今天获取验证码次数已达上限，请明天再试")

    if client_ip:
        hourly_limit = _get_int_setting("SMS_HOURLY_LIMIT_PER_IP", 30, min_value=1)
        hour_start = now - timedelta(hours=1)
        hourly_count = (
            db.query(func.count(SmsVerificationCode.id))
            .filter(
                SmsVerificationCode.client_ip == client_ip,
                SmsVerificationCode.status == "sent",
                SmsVerificationCode.created_at >= hour_start,
            )
            .scalar()
            or 0
        )
        if hourly_count >= hourly_limit:
            raise SmsRateLimitError("当前网络请求验证码过于频繁，请稍后再试")


def send_sms_code(
    db: Session,
    raw_phone: str,
    *,
    purpose: str = "login",
    client_ip: Optional[str] = None,
) -> SmsSendResult:
    phone = normalize_phone(raw_phone)
    purpose = (purpose or "login").strip().lower()
    _check_send_rate_limit(db, phone, purpose, client_ip)

    ttl_seconds = _get_int_setting("SMS_CODE_EXPIRE_SECONDS", 300, min_value=60)
    resend_seconds = _get_int_setting("SMS_RESEND_INTERVAL_SECONDS", 60, min_value=1)
    now = datetime.utcnow()
    code = _new_code()
    record = SmsVerificationCode(
        phone=phone,
        purpose=purpose,
        code_hash=_hash_code(phone, code, purpose),
        provider="smsbao",
        status="created",
        client_ip=client_ip,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    db.add(record)
    db.flush()

    try:
        if _dev_mode_enabled():
            provider_response = "dev-mode"
            print(f"[SMS][DEV] phone={phone} purpose={purpose} code={code} expires_in={ttl_seconds}s")
        else:
            provider_response = _send_by_smsbao(phone, code, ttl_seconds)
    except SmsError as exc:
        record.status = "failed"
        record.provider_response = str(exc)[:500]
        db.commit()
        raise
    except Exception as exc:
        record.status = "failed"
        record.provider_response = f"{type(exc).__name__}: {str(exc)[:460]}"
        db.commit()
        raise SmsProviderError("短信验证码发送失败，请稍后再试") from exc

    record.status = "sent"
    record.sent_at = datetime.utcnow()
    record.provider_response = provider_response
    (
        db.query(SmsVerificationCode)
        .filter(
            SmsVerificationCode.phone == phone,
            SmsVerificationCode.purpose == purpose,
            SmsVerificationCode.status == "sent",
            SmsVerificationCode.id != record.id,
        )
        .update({SmsVerificationCode.status: "superseded"}, synchronize_session=False)
    )
    db.commit()

    dev_code = code if _dev_mode_enabled() and _get_bool_setting("SMS_EXPOSE_CODE", False) else None
    return SmsSendResult(
        phone=phone,
        expire_seconds=ttl_seconds,
        resend_seconds=resend_seconds,
        dev_code=dev_code,
    )


def verify_sms_code(
    db: Session,
    raw_phone: str,
    raw_code: str,
    *,
    purpose: str = "login",
) -> str:
    phone = normalize_phone(raw_phone)
    code = (raw_code or "").strip()
    purpose = (purpose or "login").strip().lower()
    if not CODE_RE.match(code):
        raise SmsVerificationError("请输入 6 位数字验证码")

    now = datetime.utcnow()
    record = (
        db.query(SmsVerificationCode)
        .filter(
            SmsVerificationCode.phone == phone,
            SmsVerificationCode.purpose == purpose,
            SmsVerificationCode.status == "sent",
        )
        .order_by(SmsVerificationCode.created_at.desc())
        .first()
    )
    if record is None:
        raise SmsVerificationError("验证码错误或已过期")

    if record.expires_at <= now:
        record.status = "expired"
        db.commit()
        raise SmsVerificationError("验证码已过期，请重新获取")

    max_attempts = _get_int_setting("SMS_MAX_VERIFY_ATTEMPTS", 5, min_value=1)
    if (record.attempt_count or 0) >= max_attempts:
        record.status = "failed"
        db.commit()
        raise SmsVerificationError("验证码错误次数过多，请重新获取")

    if not hmac.compare_digest(record.code_hash, _hash_code(phone, code, purpose)):
        record.attempt_count = (record.attempt_count or 0) + 1
        if record.attempt_count >= max_attempts:
            record.status = "failed"
        db.commit()
        raise SmsVerificationError("验证码错误")

    record.status = "used"
    record.verified_at = now
    db.commit()
    return phone
