from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app_config import get_app_setting
from auth import ALGORITHM, SECRET_KEY, get_current_user
from database import get_db
from docmee_ppt_service import (
    create_docmee_token,
    create_portable_task,
    new_docmee_uid,
    pick_random_template,
    query_portable_task,
    refresh_ppt_download,
)
from models import Artifact, ChatMessage, Conversation, PptTaskToken, User
from permissions import record_feature_usage, require_plan_feature


router = APIRouter(prefix="/ppt", tags=["ppt"])


class PptGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    conversation_id: Optional[int] = None
    length: str = "medium"
    scene: str = "通用汇报"
    audience: str = "普通观众"
    lang: str = "zh"


class PptTaskResponse(BaseModel):
    artifact_id: int
    message_id: Optional[int] = None
    task_id: Optional[str] = None
    status: str
    progress: int = 0
    step: Optional[Any] = None
    ppt_id: Optional[str] = None
    template_id: Optional[str] = None
    file_url: Optional[str] = None
    cover_url: Optional[str] = None
    preview_url: Optional[str] = None
    error_message: Optional[str] = None
    title: Optional[str] = None


class PptDownloadResponse(BaseModel):
    artifact_id: int
    ppt_id: Optional[str] = None
    file_url: str
    name: Optional[str] = None
    subject: Optional[str] = None


class PptPreviewResponse(BaseModel):
    artifact_id: int
    ppt_id: Optional[str] = None
    preview_url: str
    preview_token: str


_ALLOWED_LENGTHS = {"short", "medium", "long"}
_ALLOWED_LANGS = {"zh", "en", "ja"}
_PPT_PREVIEW_TOKEN_MINUTES = 15


def _ppt_token_expires_at() -> datetime:
    try:
        hours = int(str(get_app_setting("DOCMEE_TOKEN_HOURS", "2") or "2").strip())
    except (TypeError, ValueError):
        hours = 2
    hours = max(1, min(48, hours))
    return datetime.utcnow() + timedelta(hours=hours)


def _store_task_token(db: Session, artifact_id: int, uid: str, token: str) -> None:
    existing = db.query(PptTaskToken).filter(PptTaskToken.artifact_id == artifact_id).first()
    if existing:
        existing.uid = uid
        existing.token = token
        existing.expires_at = _ppt_token_expires_at()
        existing.updated_at = datetime.utcnow()
        return
    db.add(PptTaskToken(
        artifact_id=artifact_id,
        uid=uid,
        token=token,
        expires_at=_ppt_token_expires_at(),
    ))


async def _get_task_token(db: Session, artifact: Artifact, uid: str) -> str:
    row = db.query(PptTaskToken).filter(PptTaskToken.artifact_id == artifact.id).first()
    if row and row.uid == uid and row.token and row.expires_at > datetime.utcnow() + timedelta(minutes=2):
        return row.token

    token = await create_docmee_token(uid, limit=1)
    _store_task_token(db, int(artifact.id), uid, token)
    db.flush()
    return token


def _assert_own_conversation(db: Session, user_id: int, conv_id: Optional[int]) -> Optional[Conversation]:
    if not conv_id:
        return None
    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.user_id == user_id,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _assert_own_ppt_artifact(db: Session, user_id: int, artifact_id: int) -> Artifact:
    artifact = db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.user_id == user_id,
        Artifact.type == "ppt",
    ).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="PPT artifact not found")
    return artifact


def _last_active_message_id(db: Session, conv_id: int) -> Optional[int]:
    last_active = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conv_id,
            ChatMessage.is_active_branch == True,  # noqa: E712
        )
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .first()
    )
    return int(last_active.id) if last_active else None


def _safe_metadata(artifact: Artifact) -> dict[str, Any]:
    metadata = artifact.artifact_metadata if isinstance(artifact.artifact_metadata, dict) else {}
    return dict(metadata)


def _ppt_payload(artifact: Artifact) -> dict[str, Any]:
    meta = _safe_metadata(artifact)
    cover_url = meta.get("cover_url")
    return {
        "type": "ppt",
        "artifact_id": int(artifact.id),
        "message_id": int(artifact.message_id) if artifact.message_id else None,
        "title": artifact.title or meta.get("prompt") or "PPT",
        "prompt": meta.get("prompt") or artifact.title or "",
        "provider": "ppt",
        "task_id": meta.get("task_id"),
        "status": meta.get("status") or "pending",
        "progress": int(meta.get("progress") or 0),
        "step": meta.get("step"),
        "ppt_id": meta.get("ppt_id"),
        "template_id": meta.get("template_id"),
        "file_url": artifact.content or meta.get("file_url"),
        "cover_url": f"/ppt/artifacts/{int(artifact.id)}/cover" if cover_url else None,
        "preview_url": meta.get("preview_url"),
        "error_message": meta.get("error_message") or meta.get("error"),
    }


def _ppt_message_content(artifact: Artifact) -> str:
    return "[[PPT]]" + json.dumps(_ppt_payload(artifact), ensure_ascii=False)


def _upsert_message_content(db: Session, artifact: Artifact) -> None:
    if not artifact.message_id:
        return
    msg = db.query(ChatMessage).filter(ChatMessage.id == artifact.message_id).first()
    if msg:
        msg.content = _ppt_message_content(artifact)


def _response_from_artifact(artifact: Artifact) -> PptTaskResponse:
    payload = _ppt_payload(artifact)
    return PptTaskResponse(
        artifact_id=payload["artifact_id"],
        message_id=payload["message_id"],
        task_id=payload["task_id"],
        status=payload["status"],
        progress=payload["progress"],
        step=payload["step"],
        ppt_id=payload["ppt_id"],
        template_id=payload["template_id"],
        file_url=payload["file_url"],
        cover_url=payload["cover_url"],
        preview_url=payload["preview_url"],
        error_message=payload["error_message"],
        title=payload["title"],
    )


def _merge_task_result(artifact: Artifact, result: dict[str, Any]) -> None:
    meta = _safe_metadata(artifact)
    status = result.get("status") or meta.get("status") or "processing"
    progress = int(result.get("progress") or meta.get("progress") or 0)
    if status == "success":
        progress = 100
    meta.update({
        "status": status,
        "progress": progress,
        "step": result.get("step"),
        "ppt_id": result.get("ppt_id") or meta.get("ppt_id"),
        "file_url": result.get("file_url") or meta.get("file_url"),
        "cover_url": result.get("cover_url") or meta.get("cover_url"),
        "preview_url": result.get("preview_url") or meta.get("preview_url"),
        "error_code": result.get("error_code"),
        "error_message": result.get("error_message"),
        "updated_at": datetime.utcnow().isoformat(),
    })
    artifact.artifact_metadata = meta
    if meta.get("file_url"):
        artifact.content = str(meta["file_url"])


def _create_preview_token(user_id: int, artifact_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "artifact_id": str(artifact_id),
        "kind": "ppt_preview",
        "exp": datetime.utcnow() + timedelta(minutes=_PPT_PREVIEW_TOKEN_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_preview_token(token: str, artifact_id: int) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Preview link expired") from exc

    if payload.get("kind") != "ppt_preview" or str(payload.get("artifact_id")) != str(artifact_id):
        raise HTTPException(status_code=401, detail="Preview link invalid")
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Preview link invalid") from exc


def _office_preview_url(file_url: str) -> str:
    return "https://view.officeapps.live.com/op/embed.aspx?src=" + quote(file_url, safe="")


async def _refresh_download_url_for_artifact(db: Session, artifact: Artifact) -> dict[str, Any]:
    meta = _safe_metadata(artifact)
    ppt_id = meta.get("ppt_id")
    uid = meta.get("uid")
    if not ppt_id:
        raise HTTPException(status_code=400, detail="PPT 尚未生成完成")
    if not uid:
        raise HTTPException(status_code=400, detail="PPT 任务信息不完整")

    token = await _get_task_token(db, artifact, str(uid))
    result = await refresh_ppt_download(token, str(ppt_id))
    meta.update({
        "ppt_id": result["ppt_id"],
        "file_url": result["file_url"],
        "name": result.get("name"),
        "subject": result.get("subject"),
        "updated_at": datetime.utcnow().isoformat(),
    })
    artifact.artifact_metadata = meta
    artifact.content = result["file_url"]
    _upsert_message_content(db, artifact)
    return result


@router.post("/generate", response_model=PptTaskResponse)
async def generate_ppt(
    body: PptGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_plan_feature(current_user, "ppt_generation", db)
    prompt = " ".join(body.prompt.split()).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="PPT 主题不能为空")

    length = body.length if body.length in _ALLOWED_LENGTHS else "medium"
    lang = body.lang if body.lang in _ALLOWED_LANGS else "zh"
    conv = _assert_own_conversation(db, int(current_user.id), body.conversation_id)

    uid = new_docmee_uid(int(current_user.id))
    token = await create_docmee_token(uid, limit=1)
    template_id = await pick_random_template(token)
    task = await create_portable_task(
        token=token,
        prompt=prompt,
        template_id=template_id,
        length=length,
        scene=(body.scene or "通用汇报")[:50],
        audience=(body.audience or "普通观众")[:50],
        lang=lang,
    )

    artifact = Artifact(
        user_id=current_user.id,
        conversation_id=conv.id if conv else None,
        type="ppt",
        title=prompt[:200],
        content=None,
        artifact_metadata={
            "provider": "ppt",
            "uid": uid,
            "task_id": task["task_id"],
            "template_id": template_id,
            "status": task["status"],
            "progress": 0,
            "prompt": prompt,
            "length": length,
            "scene": body.scene or "通用汇报",
            "audience": body.audience or "普通观众",
            "lang": lang,
            "created_at": datetime.utcnow().isoformat(),
        },
    )
    db.add(artifact)
    db.flush()
    _store_task_token(db, int(artifact.id), uid, token)

    if conv:
        msg = ChatMessage(
            conversation_id=conv.id,
            role="assistant",
            content=_ppt_message_content(artifact),
            parent_message_id=_last_active_message_id(db, conv.id),
            is_active_branch=True,
            attachments=[{
                "type": "ppt",
                "artifact_id": int(artifact.id),
                "task_id": task["task_id"],
                "status": task["status"],
                "prompt": prompt,
            }],
        )
        db.add(msg)
        db.flush()
        artifact.message_id = msg.id
        msg.content = _ppt_message_content(artifact)
        conv.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(artifact)
    record_feature_usage(current_user, "ppt_generation", db)
    return _response_from_artifact(artifact)


@router.get("/tasks/{artifact_id}", response_model=PptTaskResponse)
async def get_ppt_task(
    artifact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _assert_own_ppt_artifact(db, int(current_user.id), artifact_id)
    meta = _safe_metadata(artifact)
    uid = meta.get("uid")
    task_id = meta.get("task_id")
    if not uid or not task_id:
        raise HTTPException(status_code=400, detail="PPT 任务信息不完整")

    if meta.get("status") not in {"success", "failed"}:
        token = await _get_task_token(db, artifact, str(uid))
        result = await query_portable_task(token, str(task_id))
        _merge_task_result(artifact, result)
        _upsert_message_content(db, artifact)
        db.commit()
        db.refresh(artifact)

    return _response_from_artifact(artifact)


@router.post("/artifacts/{artifact_id}/download", response_model=PptDownloadResponse)
async def download_ppt_artifact(
    artifact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _assert_own_ppt_artifact(db, int(current_user.id), artifact_id)
    result = await _refresh_download_url_for_artifact(db, artifact)
    db.commit()

    return PptDownloadResponse(
        artifact_id=int(artifact.id),
        ppt_id=result["ppt_id"],
        file_url=result["file_url"],
        name=result.get("name"),
        subject=result.get("subject"),
    )


@router.post("/artifacts/{artifact_id}/preview", response_model=PptPreviewResponse)
async def create_ppt_preview_link(
    artifact_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _assert_own_ppt_artifact(db, int(current_user.id), artifact_id)
    meta = _safe_metadata(artifact)
    if (meta.get("status") or "").lower() != "success":
        raise HTTPException(status_code=400, detail="PPT 尚未生成完成")

    preview_token = _create_preview_token(int(current_user.id), int(artifact.id))
    preview_url = str(request.url_for("preview_ppt_artifact_page", artifact_id=int(artifact.id)))
    return PptPreviewResponse(
        artifact_id=int(artifact.id),
        ppt_id=meta.get("ppt_id"),
        preview_url=preview_url,
        preview_token=preview_token,
    )


@router.post("/artifacts/{artifact_id}/preview-page", response_class=HTMLResponse)
async def preview_ppt_artifact_page(
    artifact_id: int,
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    user_id = _decode_preview_token(token, artifact_id)
    artifact = _assert_own_ppt_artifact(db, user_id, artifact_id)
    result = await _refresh_download_url_for_artifact(db, artifact)
    db.commit()

    title = html.escape(artifact.title or result.get("subject") or "PPT 预览")
    viewer_url = html.escape(_office_preview_url(result["file_url"]), quote=True)
    download_url = html.escape(result["file_url"], quote=True)
    return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · PPT 预览</title>
  <style>
    html,body{{margin:0;height:100%;background:#f5f0e6;color:#1a1814;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}}
    .bar{{height:52px;display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid rgba(26,24,20,.1);box-sizing:border-box;background:#faf6ec;}}
    .title{{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;}}
    .btn{{border:1px solid rgba(26,24,20,.14);background:#fff;color:#1a1814;border-radius:8px;padding:7px 11px;font-size:13px;text-decoration:none;cursor:pointer;}}
    .frame{{width:100%;height:calc(100vh - 52px);border:0;display:block;background:#fff;}}
    .fallback{{position:fixed;right:14px;top:64px;font-size:12px;color:#6b6558;background:rgba(250,246,236,.94);padding:6px 10px;border-radius:8px;box-shadow:0 4px 14px rgba(26,24,20,.08);}}
  </style>
</head>
<body>
  <div class="bar">
    <button class="btn" onclick="history.back()">返回</button>
    <div class="title">{title}</div>
    <a class="btn" href="{download_url}" target="_blank" rel="noopener">下载 PPT</a>
  </div>
  <iframe class="frame" src="{viewer_url}" title="PPT 预览" allowfullscreen></iframe>
  <div class="fallback" id="fallback-tip">如果预览加载较慢，可以先下载查看。</div>
  <script>setTimeout(function(){{var el=document.getElementById('fallback-tip');if(el)el.style.display='none';}},8000);</script>
</body>
</html>""")


def _url_with_token(url: str, token: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@router.get("/artifacts/{artifact_id}/cover")
async def get_ppt_cover(
    artifact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _assert_own_ppt_artifact(db, int(current_user.id), artifact_id)
    meta = _safe_metadata(artifact)
    cover_url = meta.get("cover_url")
    uid = meta.get("uid")
    if not cover_url:
        raise HTTPException(status_code=404, detail="PPT cover not found")
    if not uid:
        raise HTTPException(status_code=400, detail="PPT 任务信息不完整")

    token = await _get_task_token(db, artifact, str(uid))
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(_url_with_token(str(cover_url), token))
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"PPT 封面图读取失败：{exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PPT 封面图 HTTP {resp.status_code}")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type") or "image/png",
    )
