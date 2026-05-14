from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import get_current_user
from chat_model_config import (
    get_available_chat_models,
    get_chat_model_config,
    get_effective_default_model,
)
from database import get_db
from models import User
from permissions import get_active_plan
from plan_config import get_free_model_ids_for_plan, get_plan_definition


router = APIRouter(prefix="/models", tags=["models"])


@router.get("/available")
def available_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan_type = get_active_plan(current_user, db)
    plan = get_plan_definition(plan_type, db)
    free_model_ids = set(get_free_model_ids_for_plan(plan_type, db))
    config = get_chat_model_config(db)
    models = get_available_chat_models(plan_type, db)
    return {
        "provider": config["provider"],
        "plan_type": plan_type,
        "plan_name": plan["name"],
        "global_default_model": config["default_model"],
        "default_model": get_effective_default_model(plan_type, db),
        "models": [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m.get("description") or "",
                "supports_vision": bool(m.get("supports_vision")),
                "quota_free": m["id"] in free_model_ids,
            }
            for m in models
        ],
    }
