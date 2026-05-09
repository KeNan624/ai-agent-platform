from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from app_config import get_app_setting


DEFAULT_DOCMEE_BASE_URL = "https://open.docmee.cn"


def _clean_base_url(value: Optional[str]) -> str:
    return (value or DEFAULT_DOCMEE_BASE_URL).strip().rstrip("/") or DEFAULT_DOCMEE_BASE_URL


def _positive_int(value: Optional[str], default: int, minimum: int = 1, maximum: int = 100) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _docmee_config() -> dict[str, Any]:
    api_key = (get_app_setting("DOCMEE_API_KEY", "") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="PPT 生成服务未配置，请先在后台系统配置中填写")
    return {
        "api_key": api_key,
        "base_url": _clean_base_url(get_app_setting("DOCMEE_BASE_URL", DEFAULT_DOCMEE_BASE_URL)),
        "token_hours": _positive_int(get_app_setting("DOCMEE_TOKEN_HOURS", "2"), 2, 1, 48),
        "template_size": _positive_int(get_app_setting("DOCMEE_TEMPLATE_SIZE", "1"), 1, 1, 30),
    }


def _unwrap_response(payload: dict[str, Any]) -> Any:
    code = payload.get("code", 0)
    if code not in (0, "0", None):
        message = payload.get("message") or payload.get("msg") or "服务请求失败"
        raise HTTPException(status_code=502, detail=f"PPT 生成服务错误：{message}")
    return payload.get("data", payload)


async def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, json=json)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"PPT 生成服务连接失败：{exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text[:500] if resp.text else resp.reason_phrase
        raise HTTPException(status_code=502, detail=f"PPT 生成服务 HTTP {resp.status_code}：{detail}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="PPT 生成服务返回了异常内容") from exc


async def create_docmee_token(uid: str, limit: int = 1) -> str:
    cfg = _docmee_config()
    payload = await _request_json(
        "POST",
        f"{cfg['base_url']}/api/user/createApiToken",
        headers={"Content-Type": "application/json", "Api-Key": cfg["api_key"]},
        json={"uid": uid, "limit": limit, "timeOfHours": cfg["token_hours"]},
    )
    data = _unwrap_response(payload)
    token = (data or {}).get("token")
    if not token:
        raise HTTPException(status_code=502, detail="PPT 生成服务临时凭证获取失败")
    return str(token)


def _find_template_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("id", "templateId", "template_id"):
            raw = value.get(key)
            if raw:
                return str(raw)
        for nested in value.values():
            found = _find_template_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_template_id(item)
            if found:
                return found
    return None


async def pick_random_template(token: str) -> str:
    cfg = _docmee_config()
    payload = await _request_json(
        "POST",
        f"{cfg['base_url']}/api/ppt/randomTemplates",
        headers={"Content-Type": "application/json", "token": token},
        json={
            "size": cfg["template_size"],
            "filters": {
                "type": 1,
                "category": None,
                "style": None,
                "themeColor": None,
                "neq_id": [],
            },
        },
    )
    data = _unwrap_response(payload)
    template_id = _find_template_id(data)
    if not template_id:
        raise HTTPException(status_code=502, detail="PPT 生成服务没有返回可用模板")
    return template_id


async def create_portable_task(
    *,
    token: str,
    prompt: str,
    template_id: str,
    length: str,
    scene: str,
    audience: str,
    lang: str,
) -> dict[str, Any]:
    cfg = _docmee_config()
    content = prompt.strip()
    if "ppt" not in content.lower() and "PPT" not in content:
        content = f"请生成一份关于「{content}」的PPT"
    payload = await _request_json(
        "POST",
        f"{cfg['base_url']}/v2/api/portable/create-task",
        headers={"Content-Type": "application/json", "token": token},
        json={
            "type": 1,
            "content": content,
            "templateId": template_id,
            "stream": False,
            "length": length,
            "scene": scene,
            "audience": audience,
            "lang": lang,
            "prompt": "语气专业，适合演示",
        },
    )
    data = _unwrap_response(payload)
    task_id = (data or {}).get("taskId")
    if not task_id:
        raise HTTPException(status_code=502, detail="PPT 生成任务创建失败")
    return {
        "task_id": str(task_id),
        "status": str((data or {}).get("status") or "pending"),
    }


async def query_portable_task(token: str, task_id: str) -> dict[str, Any]:
    cfg = _docmee_config()
    payload = await _request_json(
        "POST",
        f"{cfg['base_url']}/v2/api/portable/task-result",
        headers={"Content-Type": "application/json", "token": token},
        json={"taskId": task_id},
    )
    if payload.get("code") not in (0, "0", None):
        message = payload.get("message") or payload.get("msg") or "生成失败"
        data = payload.get("data") or {}
        if message == "failed" or (isinstance(data, dict) and data.get("status") == "failed"):
            return {
                "task_id": task_id,
                "status": "failed",
                "progress": 100,
                "step": data.get("step") if isinstance(data, dict) else None,
                "ppt_id": None,
                "file_url": None,
                "cover_url": None,
                "preview_url": None,
                "error_code": payload.get("code"),
                "error_message": "PPT 生成失败，请换个主题或稍后重试",
            }
        else:
            _unwrap_response(payload)
    else:
        data = _unwrap_response(payload) or {}
    return {
        "task_id": str(data.get("taskId") or task_id),
        "status": str(data.get("status") or "processing"),
        "progress": int(data.get("progress") or (100 if data.get("status") == "success" else 0)),
        "step": data.get("step"),
        "ppt_id": data.get("pptId"),
        "file_url": data.get("fileUrl"),
        "cover_url": data.get("coverUrl"),
        "preview_url": data.get("previewUrl"),
        "error_code": data.get("errorCode"),
        "error_message": data.get("errorMessage"),
    }


async def refresh_ppt_download(token: str, ppt_id: str) -> dict[str, Any]:
    cfg = _docmee_config()
    payload = await _request_json(
        "POST",
        f"{cfg['base_url']}/api/ppt/downloadPptx",
        headers={"Content-Type": "application/json", "token": token},
        json={"id": ppt_id, "refresh": False},
    )
    data = _unwrap_response(payload) or {}
    file_url = data.get("fileUrl")
    if not file_url:
        raise HTTPException(status_code=502, detail="PPT 下载地址获取失败")
    return {
        "ppt_id": str(data.get("id") or ppt_id),
        "name": data.get("name"),
        "subject": data.get("subject"),
        "file_url": file_url,
    }


def new_docmee_uid(user_id: int, artifact_id: Optional[int] = None) -> str:
    suffix = str(artifact_id) if artifact_id else uuid.uuid4().hex[:12]
    return f"user-{user_id}-ppt-{suffix}"
