from __future__ import annotations

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_config import get_app_setting


PLAN_CONFIG_KEY = "PLAN_CONFIG"
PLAN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PLAN_PERIODS = {"day", "month"}
FEATURE_UNLIMITED_QUOTA = -1
PLAN_FEATURE_DEFINITIONS: list[dict[str, str]] = [
    {"key": "image_generation", "label": "图片生成"},
    {"key": "video_generation", "label": "视频生成"},
    {"key": "ppt_generation", "label": "PPT 生成"},
    {"key": "web_search", "label": "联网搜索"},
    {"key": "web_scrape", "label": "网页抓取"},
    {"key": "file_upload", "label": "附件上传"},
    {"key": "custom_workspace", "label": "自建工作空间"},
]
PLAN_FEATURE_KEYS = tuple(item["key"] for item in PLAN_FEATURE_DEFINITIONS)

DEFAULT_ALLOWED_MODELS: dict[str, list[str]] = {
    "free": ["deepseek-chat"],
    "monthly": ["deepseek-chat", "claude-sonnet-4-6"],
    "quarterly": ["deepseek-chat", "claude-sonnet-4-6"],
    "yearly": ["deepseek-chat", "claude-sonnet-4-6", "claude-opus-4-6"],
}

DEFAULT_PLAN_CONFIG: dict[str, Any] = {
    "version": 1,
    "plans": [
        {
            "id": "free",
            "name": "免费版",
            "description": "体验 AI 基础能力，每天限量使用",
            "benefits": ["每天 10 次对话", "可使用基础模型", "适合轻量体验"],
            "enabled": True,
            "purchasable": False,
            "amount": "0.00",
            "duration_days": 0,
            "period": "day",
            "quota": 10,
            "allowed_models": DEFAULT_ALLOWED_MODELS["free"],
            "free_models": [],
            "enabled_features": list(PLAN_FEATURE_KEYS),
            "feature_quotas": {key: FEATURE_UNLIMITED_QUOTA for key in PLAN_FEATURE_KEYS},
            "practice_access": False,
            "practice_publish": False,
            "sort_order": 0,
        },
        {
            "id": "monthly",
            "name": "月度会员",
            "description": "日常重度使用，畅享高质量模型",
            "benefits": ["每月 500 次对话", "解锁高质量对话模型", "适合日常高频使用"],
            "enabled": True,
            "purchasable": True,
            "amount": "99.00",
            "duration_days": 30,
            "period": "month",
            "quota": 500,
            "allowed_models": DEFAULT_ALLOWED_MODELS["monthly"],
            "free_models": [],
            "enabled_features": list(PLAN_FEATURE_KEYS),
            "feature_quotas": {key: FEATURE_UNLIMITED_QUOTA for key in PLAN_FEATURE_KEYS},
            "practice_access": False,
            "practice_publish": False,
            "sort_order": 10,
        },
        {
            "id": "quarterly",
            "name": "季度会员",
            "description": "季度续费，权益默认与月度会员一致",
            "benefits": ["每月 500 次对话", "季度有效期", "权益默认与月度会员一致"],
            "enabled": True,
            "purchasable": True,
            "amount": "249.00",
            "duration_days": 90,
            "period": "month",
            "quota": 500,
            "allowed_models": DEFAULT_ALLOWED_MODELS["quarterly"],
            "free_models": [],
            "enabled_features": list(PLAN_FEATURE_KEYS),
            "feature_quotas": {key: FEATURE_UNLIMITED_QUOTA for key in PLAN_FEATURE_KEYS},
            "practice_access": False,
            "practice_publish": False,
            "sort_order": 20,
        },
        {
            "id": "yearly",
            "name": "年度会员",
            "description": "解锁全部模型和实战区权益",
            "benefits": ["每月 1000 次对话", "解锁全部对话模型", "可访问并发布实战区内容"],
            "enabled": True,
            "purchasable": True,
            "amount": "799.00",
            "duration_days": 365,
            "period": "month",
            "quota": 1000,
            "allowed_models": DEFAULT_ALLOWED_MODELS["yearly"],
            "free_models": [],
            "enabled_features": list(PLAN_FEATURE_KEYS),
            "feature_quotas": {key: FEATURE_UNLIMITED_QUOTA for key in PLAN_FEATURE_KEYS},
            "practice_access": True,
            "practice_publish": True,
            "sort_order": 30,
        },
    ],
}


def _read_raw_config(db: Optional[Session] = None) -> Optional[str]:
    if db is not None:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key = :key"),
            {"key": PLAN_CONFIG_KEY},
        ).fetchone()
        return str(row[0]).strip() if row and row[0] is not None else None
    return get_app_setting(PLAN_CONFIG_KEY, None)


def _read_chat_model_config_raw(db: Optional[Session]) -> Optional[dict[str, Any]]:
    raw: Optional[str] = None
    if db is not None:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key = 'CHAT_MODEL_CONFIG'")
        ).fetchone()
        raw = str(row[0]).strip() if row and row[0] is not None else None
    else:
        raw = get_app_setting("CHAT_MODEL_CONFIG", None)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _default_allowed_models(db: Optional[Session]) -> dict[str, list[str]]:
    mapping = {key: list(value) for key, value in DEFAULT_ALLOWED_MODELS.items()}
    raw = _read_chat_model_config_raw(db)
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        return mapping

    discovered: dict[str, list[str]] = {"free": [], "monthly": [], "quarterly": [], "yearly": []}
    enabled_ids: list[str] = []
    for item in models:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        if model_id not in enabled_ids:
            enabled_ids.append(model_id)
        allowed = item.get("allowed_plans") or []
        if not isinstance(allowed, list):
            continue
        for plan_id in allowed:
            plan_key = str(plan_id or "").strip().lower()
            if plan_key in discovered and model_id not in discovered[plan_key]:
                discovered[plan_key].append(model_id)
    if discovered["monthly"] and not discovered["quarterly"]:
        discovered["quarterly"] = list(discovered["monthly"])
    if enabled_ids and not any(discovered.values()):
        discovered["free"] = [enabled_ids[0]]
        discovered["monthly"] = list(enabled_ids)
        discovered["quarterly"] = list(enabled_ids)
        discovered["yearly"] = list(enabled_ids)
    return {key: (value or mapping[key]) for key, value in discovered.items()}


def _default_config(db: Optional[Session] = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_PLAN_CONFIG)
    allowed = _default_allowed_models(db)
    for plan in config["plans"]:
        plan["allowed_models"] = allowed.get(plan["id"], plan["allowed_models"])
        plan["free_models"] = [
            model_id for model_id in plan.get("free_models", [])
            if model_id in set(plan["allowed_models"])
        ]
    return config


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _as_amount(value: Any, default: str = "0.00") -> str:
    try:
        amount = Decimal(str(value if value is not None else default).strip() or default)
    except (InvalidOperation, ValueError):
        amount = Decimal(default)
    if amount < 0:
        amount = Decimal("0")
    return f"{amount.quantize(Decimal('0.01'))}"


def _clean_model_ids(value: Any, valid_model_ids: Optional[set[str]] = None) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        model_id = str(item or "").strip()
        if not model_id or model_id in result:
            continue
        if valid_model_ids is not None and model_id not in valid_model_ids:
            continue
        result.append(model_id)
    return result


def _clean_features(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        value = default
    result: list[str] = []
    valid = set(PLAN_FEATURE_KEYS)
    for item in value:
        key = str(item or "").strip()
        if key in valid and key not in result:
            result.append(key)
    return result


def _clean_feature_quotas(value: Any, default: Optional[dict[str, Any]] = None) -> dict[str, int]:
    default = default if isinstance(default, dict) else {}
    source = value if isinstance(value, dict) else default
    result: dict[str, int] = {}
    for key in PLAN_FEATURE_KEYS:
        raw = source.get(key, default.get(key, FEATURE_UNLIMITED_QUOTA)) if isinstance(source, dict) else FEATURE_UNLIMITED_QUOTA
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            parsed = FEATURE_UNLIMITED_QUOTA
        result[key] = max(FEATURE_UNLIMITED_QUOTA, parsed)
    return result


def _clean_benefits(value: Any, default: Optional[list[str]] = None) -> list[str]:
    source = value if isinstance(value, list) else (default if isinstance(default, list) else [])
    return [
        text for text in (str(item or "").strip() for item in source)
        if text
    ][:12]


def normalize_plan_config(
    raw: Any = None,
    *,
    db: Optional[Session] = None,
    valid_model_ids: Optional[set[str]] = None,
    strict: bool = False,
) -> dict[str, Any]:
    default = _default_config(db)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None

    raw_plans = raw.get("plans") if isinstance(raw, dict) else None
    if not isinstance(raw_plans, list):
        return default

    default_by_id = {plan["id"]: plan for plan in default["plans"]}
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []

    for idx, item in enumerate(raw_plans):
        if not isinstance(item, dict):
            continue
        plan_id = str(item.get("id") or "").strip().lower()
        if not PLAN_ID_RE.match(plan_id):
            errors.append(f"套餐 ID 无效：{plan_id or idx}")
            continue
        if plan_id in seen:
            errors.append(f"套餐 ID 重复：{plan_id}")
            continue
        seen.add(plan_id)

        base = deepcopy(default_by_id.get(plan_id) or {
            "id": plan_id,
            "name": plan_id,
            "description": "",
            "benefits": [],
            "enabled": True,
            "purchasable": True,
            "amount": "0.00",
            "duration_days": 30,
            "period": "month",
            "quota": 0,
            "allowed_models": [],
            "free_models": [],
            "enabled_features": list(PLAN_FEATURE_KEYS),
            "feature_quotas": {key: FEATURE_UNLIMITED_QUOTA for key in PLAN_FEATURE_KEYS},
            "practice_access": False,
            "practice_publish": False,
            "sort_order": len(plans) * 10,
        })

        allowed_models = _clean_model_ids(
            item.get("allowed_models", base.get("allowed_models", [])),
            valid_model_ids,
        )
        free_models = _clean_model_ids(item.get("free_models", base.get("free_models", [])), valid_model_ids)
        allowed_set = set(allowed_models)
        free_models = [model_id for model_id in free_models if model_id in allowed_set]
        enabled_features = _clean_features(
            item.get("enabled_features", base.get("enabled_features", list(PLAN_FEATURE_KEYS))),
            list(base.get("enabled_features", PLAN_FEATURE_KEYS)),
        )
        feature_quotas = _clean_feature_quotas(
            item.get("feature_quotas", base.get("feature_quotas", {})),
            base.get("feature_quotas", {}),
        )

        plan = {
            "id": plan_id,
            "name": str(item.get("name") or base.get("name") or plan_id).strip() or plan_id,
            "description": str(item.get("description") or "").strip(),
            "benefits": _clean_benefits(item.get("benefits"), base.get("benefits", [])),
            "enabled": bool(item.get("enabled", base.get("enabled", True))),
            "purchasable": bool(item.get("purchasable", base.get("purchasable", True))),
            "amount": _as_amount(item.get("amount", base.get("amount", "0.00"))),
            "duration_days": _as_int(item.get("duration_days", base.get("duration_days", 30)), int(base.get("duration_days", 30))),
            "period": str(item.get("period") or base.get("period") or "month").strip().lower(),
            "quota": _as_int(item.get("quota", base.get("quota", 0)), int(base.get("quota", 0))),
            "allowed_models": allowed_models,
            "free_models": free_models,
            "enabled_features": enabled_features,
            "feature_quotas": feature_quotas,
            "practice_access": bool(item.get("practice_access", base.get("practice_access", False))),
            "practice_publish": bool(item.get("practice_publish", base.get("practice_publish", False))),
            "sort_order": _as_int(item.get("sort_order", base.get("sort_order", len(plans) * 10)), len(plans) * 10),
        }

        if plan["period"] not in PLAN_PERIODS:
            plan["period"] = base.get("period", "month")
        if plan_id == "free":
            plan["enabled"] = True
            plan["purchasable"] = False
            plan["amount"] = "0.00"
            plan["duration_days"] = 0
        if plan["enabled"] and not plan["allowed_models"]:
            errors.append(f"套餐 {plan_id} 至少需要选择一个可用模型")
        if plan["practice_publish"]:
            plan["practice_access"] = True
        plans.append(plan)

    if "free" not in seen:
        plans.insert(0, deepcopy(default_by_id["free"]))
        if strict:
            errors.append("必须保留 free 套餐")

    if strict and errors:
        raise ValueError("；".join(errors))

    plans.sort(key=lambda item: (item["sort_order"], item["id"]))
    return {"version": 1, "plans": plans}


def get_plan_config(db: Optional[Session] = None) -> dict[str, Any]:
    return normalize_plan_config(_read_raw_config(db), db=db)


def save_plan_config(
    db: Session,
    config: dict[str, Any],
    *,
    valid_model_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    clean = normalize_plan_config(
        config,
        db=db,
        valid_model_ids=valid_model_ids,
        strict=True,
    )
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
            "key": PLAN_CONFIG_KEY,
            "value": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        },
    )
    db.commit()
    return clean


def list_plan_definitions(db: Optional[Session] = None, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    plans = get_plan_config(db)["plans"]
    if include_disabled:
        return plans
    return [plan for plan in plans if plan.get("enabled")]


def get_plan_definition(plan_type: Optional[str], db: Optional[Session] = None) -> dict[str, Any]:
    target = str(plan_type or "free").strip().lower() or "free"
    plans = get_plan_config(db)["plans"]
    plan = next((item for item in plans if item["id"] == target), None)
    if plan is not None:
        return plan
    return next((item for item in plans if item["id"] == "free"), deepcopy(DEFAULT_PLAN_CONFIG["plans"][0]))


def get_allowed_model_ids_for_plan(plan_type: Optional[str], db: Optional[Session] = None) -> list[str]:
    return list(get_plan_definition(plan_type, db).get("allowed_models") or [])


def get_free_model_ids_for_plan(plan_type: Optional[str], db: Optional[Session] = None) -> list[str]:
    return list(get_plan_definition(plan_type, db).get("free_models") or [])


def get_enabled_features_for_plan(plan_type: Optional[str], db: Optional[Session] = None) -> list[str]:
    return list(get_plan_definition(plan_type, db).get("enabled_features") or [])


def plan_has_feature(plan_type: Optional[str], feature_key: str, db: Optional[Session] = None) -> bool:
    feature = str(feature_key or "").strip()
    return feature in set(get_enabled_features_for_plan(plan_type, db))


def get_feature_quotas_for_plan(plan_type: Optional[str], db: Optional[Session] = None) -> dict[str, int]:
    plan = get_plan_definition(plan_type, db)
    return _clean_feature_quotas(plan.get("feature_quotas", {}), {})


def get_feature_quota_for_plan(plan_type: Optional[str], feature_key: str, db: Optional[Session] = None) -> int:
    feature = str(feature_key or "").strip()
    if feature not in PLAN_FEATURE_KEYS:
        return FEATURE_UNLIMITED_QUOTA
    return get_feature_quotas_for_plan(plan_type, db).get(feature, FEATURE_UNLIMITED_QUOTA)


def get_feature_label(feature_key: str) -> str:
    feature = str(feature_key or "").strip()
    found = next((item for item in PLAN_FEATURE_DEFINITIONS if item["key"] == feature), None)
    return found["label"] if found else feature


def get_feature_definitions() -> list[dict[str, str]]:
    return [dict(item) for item in PLAN_FEATURE_DEFINITIONS]


def is_model_free_for_plan(plan_type: Optional[str], model_id: str, db: Optional[Session] = None) -> bool:
    return str(model_id or "").strip() in set(get_free_model_ids_for_plan(plan_type, db))


def plan_allows_practice(plan_type: Optional[str], db: Optional[Session] = None) -> bool:
    return bool(get_plan_definition(plan_type, db).get("practice_access"))


def plan_can_publish_practice(plan_type: Optional[str], db: Optional[Session] = None) -> bool:
    return bool(get_plan_definition(plan_type, db).get("practice_publish"))


def get_purchasable_plans(db: Optional[Session] = None) -> list[dict[str, Any]]:
    return [
        plan for plan in list_plan_definitions(db, include_disabled=False)
        if plan["id"] != "free"
        and plan.get("purchasable")
        and Decimal(str(plan.get("amount") or "0")) > 0
        and int(plan.get("duration_days") or 0) > 0
    ]


def get_grantable_plans(db: Optional[Session] = None) -> list[dict[str, Any]]:
    return [
        plan for plan in list_plan_definitions(db, include_disabled=False)
        if plan["id"] != "free" and int(plan.get("duration_days") or 0) > 0
    ]
