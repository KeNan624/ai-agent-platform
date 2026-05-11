import os
import sys
import json
import html
import time
from contextlib import asynccontextmanager
from typing import Optional, Union

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

load_dotenv()

from agent.core import run_agent, run_project_agent  # noqa: E402 (after load_dotenv)
from auth import ALGORITHM, SECRET_KEY, get_current_user  # noqa: E402
from chat_model_config import get_effective_default_model  # noqa: E402
from context_manager import build_conversation_context  # noqa: E402
from database import Base, engine, get_db  # noqa: E402
from models import ChatMessage, Conversation, User  # noqa: E402
from permissions import check_permission, get_active_plan, record_usage  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import member as member_router  # noqa: E402
from routers import payment as payment_router  # noqa: E402
from routers import admin as admin_router  # noqa: E402
from routers import conversations as conversations_router  # noqa: E402
from routers import projects as projects_router  # noqa: E402
from routers import media as media_router  # noqa: E402
from routers import ppt as ppt_router  # noqa: E402
from routers import model_config as model_config_router  # noqa: E402
from routers import practice as practice_router  # noqa: E402
from routers import courses as courses_router  # noqa: E402
from routers import admin_courses as admin_courses_router  # noqa: E402
from seeds import seed_preset_projects  # noqa: E402


def ensure_practice_column_schema() -> None:
    """Lightweight additive migration for the 实战区课程/专栏 shared tables."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "practice_courses" not in table_names or "practice_lessons" not in table_names:
        return

    course_cols = {col["name"] for col in inspector.get_columns("practice_courses")}
    lesson_cols = {col["name"] for col in inspector.get_columns("practice_lessons")}

    with engine.begin() as conn:
        if "content_kind" not in course_cols:
            conn.execute(text(
                "ALTER TABLE practice_courses "
                "ADD COLUMN content_kind VARCHAR(16) NOT NULL DEFAULT 'course'"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_practice_courses_content_kind "
                "ON practice_courses (content_kind)"
            ))
        if "source_type" not in lesson_cols:
            conn.execute(text("ALTER TABLE practice_lessons ADD COLUMN source_type VARCHAR(32)"))
        if "source_url" not in lesson_cols:
            conn.execute(text("ALTER TABLE practice_lessons ADD COLUMN source_url TEXT"))
        if "source_doc_id" not in lesson_cols:
            conn.execute(text("ALTER TABLE practice_lessons ADD COLUMN source_doc_id VARCHAR(128)"))


def ensure_practice_project_schema() -> None:
    """Lightweight migration for existing 实战区项目 databases."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "practice_projects" not in table_names:
        return

    project_cols = {col["name"] for col in inspector.get_columns("practice_projects")}
    dialect = engine.dialect.name

    with engine.begin() as conn:
        if "is_featured" not in project_cols:
            default_value = "0" if dialect == "sqlite" else "false"
            conn.execute(text(
                "ALTER TABLE practice_projects "
                f"ADD COLUMN is_featured BOOLEAN NOT NULL DEFAULT {default_value}"
            ))
            return

        conn.execute(text("UPDATE practice_projects SET is_featured = false WHERE is_featured IS NULL"))
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE practice_projects ALTER COLUMN is_featured SET DEFAULT false"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all database tables on startup
    Base.metadata.create_all(bind=engine)
    ensure_practice_project_schema()
    ensure_practice_column_schema()

    # Seed system preset projects (idempotent)
    seed_preset_projects()

    # Install Playwright browsers on first run if missing
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
        capture_output=True,
    )
    yield


app = FastAPI(
    title="AI Agent Platform",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str  # "user" or "assistant"
    content: Union[str, list[dict]]


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = []
    model: str = "deepseek-chat"
    system_prompt: str = ""  # Agent 的身份设定（legacy）
    agent_type: str = "simple"  # 默认通用助手；四阶段 Agent 仍可显式传 side_hustle
    project_id: Optional[int] = None  # 新：优先用 project_id，从数据库读配置
    conversation_id: Optional[int] = None  # 新：如果指定，消息保存到该对话
    # 🖼📄 阶段二 2.1/2.2: 附件(图片 + 文档)· 前端上传的图片 base64 / 前端解析出的文档文字
    # 格式(kind 字段区分类型):
    #   图片: {"kind":"image","media_type":"image/jpeg","data":"base64","name":"xxx.jpg"}
    #   文档: {"kind":"document","name":"xxx.pdf","extracted_text":"全文","meta":{"pages":12,"chars":8500,"format":"pdf","truncated":false}}
    # 兼容旧格式(v18):若无 kind 字段 · 按 image 处理
    attachments: Optional[list[dict]] = None


class ToolCall(BaseModel):
    name: str
    input: dict
    result: str


class ChatResponse(BaseModel):
    response: str
    history: list[Message]
    tool_calls: list[ToolCall]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GENERAL_ASSISTANT_PROMPT = """你是「阿川 AI 超级助手」，也可以简称「AI 阿川」。
你的定位是阿川平台里的通用 AI 工作台助手。

回答规则：
- 用户问你是谁、你的身份、你的名字时，明确回答：我是阿川 AI 超级助手。
- 默认用中文，表达直接、清楚、实用。
- 不要自称数据分析师、小数、副业顾问或任何专业角色，除非用户主动进入对应项目。
- 可以帮助用户聊天、写作、搜索、分析、编程、生成图片和视频。
"""

def _is_general_agent(agent_type: Optional[str]) -> bool:
    raw = (agent_type or "").strip().lower()
    return raw in {"", "simple", "general", "chat", "default", "home", "preset_analyst"} or raw.startswith("simple:")

def _wrap_message_with_prompt(req: ChatRequest, history_len: int) -> str:
    """Keep role prompts hidden; system_prompt is passed as system instructions."""
    return req.message


def _admin_default_chat_model(user: User, db: Session) -> str:
    """Resolve the backend-controlled default model for ordinary conversations."""
    plan_type = get_active_plan(user, db)
    return get_effective_default_model(plan_type, db)


# ---------------------------------------------------------------------------
# Routes (API routes MUST come before static file mount)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Non-streaming chat endpoint (kept for backward compatibility).

    Prefer /chat/stream for the user-facing web UI to get live typing.
    """
    current_user_id = int(current_user.id)
    effective_model = _admin_default_chat_model(current_user, db)
    check_permission(current_user, effective_model, db)

    raw_history = [{"role": m.role, "content": m.content} for m in req.history]
    user_message = _wrap_message_with_prompt(req, len(raw_history))
    context = build_conversation_context(
        db=db,
        user_id=current_user_id,
        conversation_id=req.conversation_id,
        history=raw_history,
        current_user_message=user_message,
    )
    history = context.history

    final_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    pending_inputs: dict[str, dict] = {}

    try:
        async for event in run_agent(
            user_message,
            agent_type=req.agent_type,
            conversation_history=history,
            model=effective_model,
        ):
            if event["type"] == "text":
                final_text_parts.append(event["content"])
            elif event["type"] == "tool_call":
                data = event["content"]
                pending_inputs[data["name"]] = data["input"]
            elif event["type"] == "tool_result":
                data = event["content"]
                tool_calls.append(
                    ToolCall(
                        name=data["name"],
                        input=pending_inputs.pop(data["name"], {}),
                        result=data["result"],
                    )
                )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        import traceback
        print("===== /chat error =====", flush=True)
        traceback.print_exc()
        print("========================", flush=True)
        raise HTTPException(status_code=500, detail=f"Agent error: {type(e).__name__}: {str(e)}")

    record_usage(current_user, effective_model, db)

    updated_history = list(req.history) + [
        Message(role="user", content=req.message),
        Message(role="assistant", content="\n".join(final_text_parts)),
    ]

    return ChatResponse(
        response="\n".join(final_text_parts),
        history=updated_history,
        tool_calls=tool_calls,
    )


# ---------------------------------------------------------------------------
# Streaming chat endpoint (SSE)
# ---------------------------------------------------------------------------

@app.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Streaming chat endpoint using Server-Sent Events (SSE).

    支持两种调用：
      1. 新：传 project_id → 从数据库读 project 配置，走 run_project_agent
      2. 旧：传 agent_type → 走 run_agent（兼容）

    Response format: each event is a line `data: {json}\n\n`.
    """
    current_user_id = int(current_user.id)
    # Resolve project (if given)
    project = None
    if req.project_id:
        from models import Project
        from sqlalchemy import or_
        project = db.query(Project).filter(
            Project.id == req.project_id,
            or_(Project.user_id == current_user_id, Project.user_id.is_(None)),
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

    # Ordinary conversations ignore the client-provided model. Admin controls
    # the default in 后台管理 -> 系统配置 -> 对话模型管理.
    effective_model = project.model if project else _admin_default_chat_model(current_user, db)
    print(
        f"[CHAT] /chat/stream user={current_user_id} "
        f"req_model={req.model} effective_model={effective_model} "
        f"project_id={req.project_id} agent_type={req.agent_type}",
        flush=True,
    )
    check_permission(current_user, effective_model, db)

    raw_history = [{"role": m.role, "content": m.content} for m in req.history]
    user_message = _wrap_message_with_prompt(req, len(raw_history))
    context = build_conversation_context(
        db=db,
        user_id=current_user_id,
        conversation_id=req.conversation_id,
        history=raw_history,
        current_user_message=user_message,
    )
    history = context.history

    async def event_generator():
        full_text_parts: list[str] = []
        pending_delta_text = ""
        last_delta_flush = time.monotonic()
        try:
            # Choose agent generator based on whether project is specified
            if project is not None:
                agent_stream = run_project_agent(
                    user_message,
                    project=project,
                    conversation_history=history,
                    attachments=req.attachments,  # 🖼 阶段二 2.1: 透传图片附件
                    user_id=current_user_id,  # 🎬 v24 · P1.5 视频成本拦截需要
                    db=db,                     # 🎬 v24 · P1.5 视频成本拦截需要
                )
            else:
                if _is_general_agent(req.agent_type):
                    system_prompt = (req.system_prompt or "").strip() or GENERAL_ASSISTANT_PROMPT
                    agent_stream = run_project_agent(
                        user_message,
                        project={
                            "mode": "simple",
                            "model": effective_model,
                            "system_prompt": system_prompt,
                        },
                        conversation_history=history,
                        attachments=req.attachments,
                        user_id=current_user_id,
                        db=db,
                    )
                else:
                    agent_stream = run_agent(
                        user_message,
                        agent_type=req.agent_type,
                        conversation_history=history,
                        model=effective_model,
                        attachments=req.attachments,  # 🖼 阶段二 2.1: 透传图片附件
                    )

            async for event in agent_stream:
                etype = event["type"]

                if etype == "text_delta":
                    # Claude often emits 1-3 Chinese chars per delta. Coalesce tiny chunks
                    # so the UI paints in smooth phrases instead of reparsing per character.
                    pending_delta_text += event.get("content", "")
                    now = time.monotonic()
                    if len(pending_delta_text) >= 18 or (now - last_delta_flush) >= 0.045:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                        last_delta_flush = now

                elif etype == "text":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    # full text block completed; accumulate for final history
                    full_text_parts.append(event["content"])
                    yield f"data: {json.dumps({'type': 'text', 'text': event['content']}, ensure_ascii=False)}\n\n"

                elif etype == "tool_call":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    data = event["content"]
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': data['name'], 'input': data['input']}, ensure_ascii=False)}\n\n"

                elif etype == "tool_result":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    data = event["content"]
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': data['name'], 'result': data['result']}, ensure_ascii=False)}\n\n"

                # 🌐 v21: 联网搜索累积的引用来源(发给前端渲染卡片)
                elif etype == "citations":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    yield f"data: {json.dumps({'type': 'citations', 'items': event.get('content', [])}, ensure_ascii=False)}\n\n"

                # 🎨 v23: P1 工坊融入主对话 · AI 调画图工具后 · 图片 URL 直接发前端渲染
                elif etype == "image_generated":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    payload = event.get("content", {}) or {}
                    yield f"data: {json.dumps({'type': 'image_generated', 'urls': payload.get('urls', []), 'prompt': payload.get('prompt', ''), 'aspect_ratio': payload.get('aspect_ratio', '1:1')}, ensure_ascii=False)}\n\n"

                # 🎬 v24: P1.5 视频融入主对话 · AI 调视频工具后立刻发 task_id 给前端 · 前端开始轮询
                elif etype == "video_started":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    payload = event.get("content", {}) or {}
                    yield f"data: {json.dumps({'type': 'video_started', 'task_id': payload.get('task_id', ''), 'prompt': payload.get('prompt', ''), 'duration': payload.get('duration', 5), 'aspect_ratio': payload.get('aspect_ratio', '16:9'), 'estimated_seconds_to_finish': payload.get('estimated_seconds_to_finish', 90)}, ensure_ascii=False)}\n\n"

                # 📎 v26: P2.A URL 附件 · AI 调 scrape_webpage 后 · 把 url + title 发给前端展示来源卡
                elif etype == "url_scraped":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    payload = event.get("content", {}) or {}
                    yield f"data: {json.dumps({'type': 'url_scraped', 'url': payload.get('url', ''), 'title': payload.get('title', ''), 'content_length': payload.get('content_length', 0), 'error': payload.get('error')}, ensure_ascii=False)}\n\n"

                # ─── NEW: 4-stage event types ───
                elif etype in (
                    "plan_start", "plan",
                    "step_start", "step_done",
                    "reflect_start", "reflection",
                    "recommend_start", "recommend",
                ):
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    payload = {"type": etype, "content": event.get("content")}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif etype == "error":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    err_msg = event.get("content", {}).get("message", "Agent 执行失败")
                    yield f"data: {json.dumps({'type': 'error', 'message': err_msg}, ensure_ascii=False)}\n\n"

                elif etype == "done":
                    if pending_delta_text:
                        yield f"data: {json.dumps({'type': 'delta', 'text': pending_delta_text}, ensure_ascii=False)}\n\n"
                        pending_delta_text = ""
                    # Record usage now that the call succeeded
                    try:
                        record_usage(current_user, effective_model, db)
                    except Exception as e:
                        print(f"record_usage failed: {e}", flush=True)

                    full_text = "\n".join(full_text_parts)
                    yield f"data: {json.dumps({'type': 'done', 'full_text': full_text}, ensure_ascii=False)}\n\n"

        except Exception as e:
            import traceback
            print("===== /chat/stream error =====", flush=True)
            traceback.print_exc()
            print("==============================", flush=True)
            err = f"{type(e).__name__}: {str(e)}"
            yield f"data: {json.dumps({'type': 'error', 'message': err}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering when deployed
        },
    )


# ---------------------------------------------------------------------------
# Sub-routers (must be before static file mount)
# ---------------------------------------------------------------------------

app.include_router(auth_router.router)
app.include_router(member_router.router)
app.include_router(payment_router.router)
app.include_router(admin_router.router)
app.include_router(conversations_router.router)
app.include_router(projects_router.router)
app.include_router(media_router.router)
app.include_router(ppt_router.router)
app.include_router(model_config_router.router)
app.include_router(practice_router.router)
app.include_router(courses_router.router)
app.include_router(admin_courses_router.router)

# ---------------------------------------------------------------------------
# Static files (MUST come last, otherwise it intercepts all requests)
# ---------------------------------------------------------------------------

# Root route → landing page
@app.get("/")
async def root():
    return FileResponse("frontend/landing.html")


@app.get("/share/{share_token}", response_class=HTMLResponse)
async def share_page(share_token: str, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(share_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=404, detail="Share link not found")

    if payload.get("type") != "conversation_share":
        raise HTTPException(status_code=404, detail="Share link not found")

    conv_id = payload.get("conv_id")
    user_id = payload.get("user_id")
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conv_id, Conversation.user_id == user_id)
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Share link not found")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv.id, ChatMessage.is_active_branch == True)  # noqa: E712
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )
    title = html.escape(conv.title or "对话记录")
    body = []
    for msg in messages:
        role = "我" if msg.role == "user" else "阿川 AI"
        content = html.escape(msg.content or "").replace("\n", "<br>")
        body.append(
            f'<section class="msg {html.escape(msg.role)}">'
            f'<div class="role">{role}</div>'
            f'<div class="content">{content}</div>'
            "</section>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} · 阿川 AI 分享</title>
  <style>
    body {{ margin:0; background:#fbf7ef; color:#1f1b16; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:760px; margin:0 auto; padding:42px 18px 80px; }}
    h1 {{ font-size:28px; line-height:1.25; margin:0 0 8px; }}
    .meta {{ color:#8b8172; font-size:13px; margin-bottom:28px; }}
    .msg {{ background:#fff; border:1px solid #eee2cf; border-radius:12px; padding:16px; margin:14px 0; }}
    .msg.user {{ background:#fff8eb; }}
    .role {{ font-weight:700; color:#9a6a24; font-size:13px; margin-bottom:8px; }}
    .content {{ font-size:15px; line-height:1.75; overflow-wrap:anywhere; }}
    footer {{ margin-top:28px; color:#9a9488; font-size:12px; text-align:center; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <div class="meta">阿川 AI 超级助手 · 只读分享</div>
    {''.join(body) if body else '<section class="msg"><div class="content">这条对话暂无内容。</div></section>'}
    <footer>来自阿川 AI 超级助手</footer>
  </main>
</body>
</html>"""


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/admin", StaticFiles(directory="admin", html=True), name="admin")
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
