from __future__ import annotations

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_config import get_app_setting
from plan_config import PLAN_FEATURE_KEYS


CREDIT_PACKAGE_CONFIG_KEY = "CREDIT_PACKAGE_CONFIG"
CREDIT_BILLING_CONFIG_KEY = "CREDIT_BILLING_CONFIG"
CREDIT_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DEFAULT_MODEL_CREDIT_PRICES: dict[str, int] = {
    "deepseek-chat": 1,
    "gpt-4o-mini": 1,
    "claude-sonnet-4-6": 1,
    "gemini-pro": 2,
    "gpt-4o": 3,
    "gemini-ultra": 8,
    "claude-opus-4-6": 10,
}

DEFAULT_FEATURE_CREDIT_PRICES: dict[str, int] = {
    "image_generation": 10,
    "video_generation": 50,
    "ppt_generation": 20,
    "web_search": 1,
    "web_scrape": 1,
    "file_upload": 0,
    "custom_workspace": 0,
}

DEFAULT_CREDIT_PACKAGE_CONFIG: dict[str, Any] = {
    "version": 1,
    "packages": [
        {
            "id": "credits_100",
            "name": "100 积分包",
            "description": "示例积分包，默认下架，启用后前台可购买。",
            "enabled": False,
            "amount": "19.90",
            "credits": 100,
            "sort_order": 10,
        },
        {
            "id": "credits_500",
            "name": "500 积分包",
            "description": "示例积分包，默认下架，启用后前台可购买。",
            "enabled": False,
            "amount": "89.90",
            "credits": 500,
            "sort_order": 20,
        },
    ],
}

DEFAULT_CREDIT_BILLING_CONFIG: dict[str, Any] = {
    "version": 1,
    "model_prices": DEFAULT_MODEL_CREDIT_PRICES,
    "feature_prices": DEFAULT_FEATURE_CREDIT_PRICES,
}


def _read_raw_config(key: str, db: Optional[Session] = None) -> Optional[str]:
    if db is not None:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key = :key"),
            {"key": key},
        ).fetchone()
        return str(row[0]).strip() if row and row[0] is not None else None
    return get_app_setting(key, None)


def _write_config(db: Session, key: str, config: dict[str, Any]) -> None:
    db.execute(
        text("""
            INSERT INTO app_settings (key, value, is_secret, updated_at)
            VALUES (:key, :value, false, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                is_secret = false,
                updated_at = NOW()
        """),
        {
            "key": key,
            "value": json.dumps(config, ensure_ascii=False, separators=(",", ":")),
        },
    )


def _as_amount(value: Any, default: str = "0.00") -> str:
    try:
        amount = Decimal(str(value if value is not None else default).strip() or default)
    except (InvalidOperation, ValueError):
        amount = Decimal(default)
    if amount < 0:
        amount = Decimal("0")
    return f"{amount.quantize(Decimal('0.01'))}"


def _as_int(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def normalize_credit_package_config(raw: Any = None, *, strict: bool = False) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    raw_packages = raw.get("packages") if isinstance(raw, dict) else None
    if not isinstance(raw_packages, list):
        return deepcopy(DEFAULT_CREDIT_PACKAGE_CONFIG)

    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for idx, item in enumerate(raw_packages):
        if not isinstance(item, dict):
            continue
        package_id = str(item.get("id") or "").strip().lower()
        if not CREDIT_PACKAGE_ID_RE.match(package_id):
            errors.append(f"积分包 ID 无效：{package_id or idx}")
            continue
        if package_id in seen:
            errors.append(f"积分包 ID 重复：{package_id}")
            continue
        seen.add(package_id)
        package = {
            "id": package_id,
            "name": str(item.get("name") or package_id).strip() or package_id,
            "description": str(item.get("description") or "").strip(),
            "enabled": bool(item.get("enabled", False)),
            "amount": _as_amount(item.get("amount", "0.00")),
            "credits": _as_int(item.get("credits"), 0),
            "sort_order": _as_int(item.get("sort_order"), len(packages) * 10),
        }
        if package["enabled"] and Decimal(package["amount"]) <= 0:
            errors.append(f"积分包 {package_id} 启用时价格必须大于 0")
        if package["enabled"] and package["credits"] <= 0:
            errors.append(f"积分包 {package_id} 启用时积分数必须大于 0")
        packages.append(package)

    if strict and errors:
        raise ValueError("；".join(errors))
    packages.sort(key=lambda item: (item["sort_order"], item["id"]))
    return {"version": 1, "packages": packages}


def _enabled_model_ids(db: Optional[Session]) -> set[str]:
    try:
        from chat_model_config import get_chat_model_config

        return {
            str(model.get("id") or "").strip()
            for model in get_chat_model_config(db).get("models", [])
            if str(model.get("id") or "").strip() and model.get("enabled")
        }
    except Exception:
        return set(DEFAULT_MODEL_CREDIT_PRICES)


def normalize_credit_billing_config(
    raw: Any = None,
    *,
    db: Optional[Session] = None,
    valid_model_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None

    model_ids = set(valid_model_ids) if valid_model_ids is not None else _enabled_model_ids(db)
    model_prices: dict[str, int] = {}
    raw_model_prices = raw.get("model_prices") if isinstance(raw, dict) else None
    raw_model_prices = raw_model_prices if isinstance(raw_model_prices, dict) else {}
    for model_id in sorted(model_ids):
        model_prices[model_id] = _as_int(
            raw_model_prices.get(model_id, DEFAULT_MODEL_CREDIT_PRICES.get(model_id, 0)),
            DEFAULT_MODEL_CREDIT_PRICES.get(model_id, 0),
        )

    feature_prices: dict[str, int] = {}
    raw_feature_prices = raw.get("feature_prices") if isinstance(raw, dict) else None
    raw_feature_prices = raw_feature_prices if isinstance(raw_feature_prices, dict) else {}
    for feature_key in PLAN_FEATURE_KEYS:
        feature_prices[feature_key] = _as_int(
            raw_feature_prices.get(feature_key, DEFAULT_FEATURE_CREDIT_PRICES.get(feature_key, 0)),
            DEFAULT_FEATURE_CREDIT_PRICES.get(feature_key, 0),
        )

    return {
        "version": 1,
        "model_prices": model_prices,
        "feature_prices": feature_prices,
    }


def get_credit_package_config(db: Optional[Session] = None) -> dict[str, Any]:
    return normalize_credit_package_config(_read_raw_config(CREDIT_PACKAGE_CONFIG_KEY, db))


def save_credit_package_config(db: Session, config: dict[str, Any]) -> dict[str, Any]:
    clean = normalize_credit_package_config(config, strict=True)
    _write_config(db, CREDIT_PACKAGE_CONFIG_KEY, clean)
    db.commit()
    return clean


def get_credit_billing_config(db: Optional[Session] = None) -> dict[str, Any]:
    return normalize_credit_billing_config(_read_raw_config(CREDIT_BILLING_CONFIG_KEY, db), db=db)


def save_credit_billing_config(
    db: Session,
    config: dict[str, Any],
    *,
    valid_model_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    clean = normalize_credit_billing_config(config, db=db, valid_model_ids=valid_model_ids)
    _write_config(db, CREDIT_BILLING_CONFIG_KEY, clean)
    db.commit()
    return clean


def get_purchasable_credit_packages(db: Optional[Session] = None) -> list[dict[str, Any]]:
    return [
        package for package in get_credit_package_config(db)["packages"]
        if package.get("enabled")
        and Decimal(str(package.get("amount") or "0")) > 0
        and int(package.get("credits") or 0) > 0
    ]


def get_credit_package(package_id: str, db: Optional[Session] = None) -> Optional[dict[str, Any]]:
    target = str(package_id or "").strip().lower()
    return next((item for item in get_credit_package_config(db)["packages"] if item["id"] == target), None)


def get_model_credit_price(model_id: str, db: Optional[Session] = None) -> int:
    return int(get_credit_billing_config(db).get("model_prices", {}).get(str(model_id or "").strip(), 0) or 0)


def get_feature_credit_price(feature_key: str, db: Optional[Session] = None) -> int:
    return int(get_credit_billing_config(db).get("feature_prices", {}).get(str(feature_key or "").strip(), 0) or 0)
