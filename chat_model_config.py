from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_config import get_app_setting
from database import SessionLocal
from plan_config import (
    get_allowed_model_ids_for_plan,
    is_model_free_for_plan,
)


CHAT_MODEL_CONFIG_KEY = "CHAT_MODEL_CONFIG"


DEFAULT_CHAT_MODEL_CONFIG: dict[str, Any] = {
    "provider": {
        "id": "ephone_anthropic",
        "label": "ePhone / Anthropic 兼容",
        "protocol": "anthropic_messages",
    },
    "default_model": "claude-sonnet-4-6",
    "models": [
        {
            "id": "deepseek-chat",
            "name": "DeepSeek",
            "description": "轻量对话模型",
            "enabled": True,
            "supports_vision": False,
            "allowed_plans": ["free", "monthly", "yearly"],
        },
        {
            "id": "claude-sonnet-4-6",
            "name": "Claude Sonnet 4.6",
            "description": "平衡 · 推荐",
            "enabled": True,
            "supports_vision": True,
            "allowed_plans": ["monthly", "yearly"],
        },
        {
            "id": "claude-opus-4-6",
            "name": "Claude Opus 4.6",
            "description": "高质量 · 年度会员",
            "enabled": True,
            "supports_vision": True,
            "allowed_plans": ["yearly"],
        },
    ],
}


def _read_raw_config(db: Optional[Session] = None) -> Optional[str]:
    if db is not None:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key = :key"),
            {"key": CHAT_MODEL_CONFIG_KEY},
        ).fetchone()
        return str(row[0]).strip() if row and row[0] is not None else None
    return get_app_setting(CHAT_MODEL_CONFIG_KEY, None)


def _clean_model(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return None

    allowed = raw.get("allowed_plans") or []
    if not isinstance(allowed, list):
        allowed = []
    clean_allowed: list[str] = []
    for item in allowed:
        plan_id = str(item or "").strip().lower()
        if plan_id and plan_id not in clean_allowed:
            clean_allowed.append(plan_id)

    return {
        "id": model_id,
        "name": str(raw.get("name") or model_id).strip() or model_id,
        "description": str(raw.get("description") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "supports_vision": bool(raw.get("supports_vision", False)),
        "allowed_plans": clean_allowed,
    }


def normalize_chat_model_config(raw: Any = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CHAT_MODEL_CONFIG)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None

    if isinstance(raw, dict):
        default_model = str(raw.get("default_model") or "").strip()
        if default_model:
            config["default_model"] = default_model

        raw_models = raw.get("models")
        if isinstance(raw_models, list):
            models = []
            seen = set()
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                clean = _clean_model(item)
                if not clean or clean["id"] in seen:
                    continue
                seen.add(clean["id"])
                models.append(clean)
            if models:
                config["models"] = models

    ids = {m["id"] for m in config["models"]}
    if config["default_model"] not in ids:
        first_enabled = next((m["id"] for m in config["models"] if m["enabled"]), None)
        config["default_model"] = first_enabled or config["models"][0]["id"]

    return config


def get_chat_model_config(db: Optional[Session] = None) -> dict[str, Any]:
    return normalize_chat_model_config(_read_raw_config(db))


def save_chat_model_config(db: Session, config: dict[str, Any]) -> dict[str, Any]:
    clean = normalize_chat_model_config(config)
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
            "key": CHAT_MODEL_CONFIG_KEY,
            "value": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        },
    )
    db.commit()
    return clean


def get_model_by_id(model_id: str, db: Optional[Session] = None) -> Optional[dict[str, Any]]:
    config = get_chat_model_config(db)
    target = str(model_id or "").strip()
    return next((m for m in config["models"] if m["id"] == target), None)


def get_available_chat_models(plan_type: str, db: Optional[Session] = None) -> list[dict[str, Any]]:
    plan = str(plan_type or "free").strip() or "free"
    config = get_chat_model_config(db)
    allowed_model_ids = set(get_allowed_model_ids_for_plan(plan, db))
    return [
        m for m in config["models"]
        if m.get("enabled") and m.get("id") in allowed_model_ids
    ]


def get_available_model_ids(plan_type: str, db: Optional[Session] = None) -> list[str]:
    return [m["id"] for m in get_available_chat_models(plan_type, db)]


def is_model_allowed_for_plan(model_id: str, plan_type: str, db: Optional[Session] = None) -> bool:
    model = get_model_by_id(model_id, db)
    if not model or not model.get("enabled"):
        return False
    return model["id"] in set(get_allowed_model_ids_for_plan(plan_type, db))


def is_model_quota_free_for_plan(model_id: str, plan_type: str, db: Optional[Session] = None) -> bool:
    model = get_model_by_id(model_id, db)
    if not model or not model.get("enabled"):
        return False
    return is_model_free_for_plan(plan_type, model["id"], db)


def get_effective_default_model(plan_type: Optional[str] = None, db: Optional[Session] = None) -> str:
    config = get_chat_model_config(db)
    if plan_type:
        available = get_available_chat_models(plan_type, db)
        ids = {m["id"] for m in available}
        if config["default_model"] in ids:
            return config["default_model"]
        if available:
            return available[0]["id"]
    return config["default_model"]


def get_chat_provider_status(db: Optional[Session] = None) -> dict[str, Any]:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        from app_config import get_api_config_items

        items = {item["key"]: item for item in get_api_config_items(db)}
        api_key = items.get("ANTHROPIC_API_KEY", {})
        base_url = items.get("ANTHROPIC_BASE_URL", {})
        return {
            "id": "ephone_anthropic",
            "label": "ePhone / Anthropic 兼容",
            "protocol": "anthropic_messages",
            "api_key": {
                "key": "ANTHROPIC_API_KEY",
                "has_value": bool(api_key.get("has_value")),
                "masked_value": api_key.get("masked_value") or "",
                "source": api_key.get("source") or "empty",
            },
            "base_url": {
                "key": "ANTHROPIC_BASE_URL",
                "value": base_url.get("value") or "",
                "source": base_url.get("source") or "empty",
                "placeholder": "https://api.ephone.ai",
            },
        }
    finally:
        if close_db:
            db.close()
