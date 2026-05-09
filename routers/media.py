"""
Media Router · 图片 / 视频生成 + 意图识别
=========================================

支持的接入：
  图片（通过 ePhone 中转）：
    - gpt-image-2                  (默认)
    - gemini-2.5-flash-image       (Nano Banana，¥0.042/次)
    - gemini-3-pro-image-preview   (Nano Banana Pro，高质量)
    - imagen-4.0-fast-generate-001 (Imagen Fast)
  视频（通过火山方舟官方）：
    - doubao-seedance-2-0-fast-260128  (字节 Seedance 2.0 Fast，默认)

环境变量（.env 里填）：
  EPHONE_API_KEY=sk-...                       # ePhone 图片 key
  EPHONE_BASE_URL=https://api.ephone.ai       # （可选，默认即此值）
  ARK_API_KEY=xxx-xxx-xxx-xxx-xxx             # 火山方舟 UUID key
  ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3  # （可选）

如果没配 key，会自动回退到占位图/占位视频（开发期用）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

# 防御式加载 .env —— 即便主 main.py 没调用 load_dotenv，这里也会兜底读一次
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app_config import get_app_setting
from auth import get_current_user
from database import get_db
from models import Artifact, ChatMessage, Conversation, User

# ═════ 启动时打印 key 状态，方便排查 ═════
def _mask(s: Optional[str]) -> str:
    if not s:
        return "<NOT SET>"
    s = s.strip()
    if len(s) < 10:
        return "<TOO SHORT>"
    return f"{s[:8]}...{s[-4:]} (len={len(s)})"


def _exception_detail(e: Exception) -> str:
    """Return a useful error string even for exceptions whose str(e) is empty."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__

print(f"[MEDIA] EPHONE_API_KEY = {_mask(get_app_setting('EPHONE_API_KEY', ''))}")
print(f"[MEDIA] EPHONE_BASE_URL = {get_app_setting('EPHONE_BASE_URL', 'https://api.ephone.ai')}")
print(f"[MEDIA] ARK_API_KEY    = {_mask(get_app_setting('ARK_API_KEY', ''))}")
print(f"[MEDIA] ARK_BASE_URL   = {get_app_setting('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')}")

router = APIRouter(prefix="/media", tags=["media"])
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_TASK_TIMEOUT_SECONDS = 240


# ══════════════════════════════════════════════════════════
#  Pydantic Schemas
# ══════════════════════════════════════════════════════════

class IntentCheckRequest(BaseModel):
    text: str
    # 可选：对话上下文，帮助 Haiku 判断更准
    recent_history: Optional[list[dict]] = None


class IntentCheckResponse(BaseModel):
    intent: str  # "image" | "video" | "chat"
    confidence: float  # 0.0 - 1.0
    prompt: Optional[str] = None  # 如果是 image/video，提取出的提示词
    reason: Optional[str] = None  # 判断理由（调试用）


class ImageGenerateRequest(BaseModel):
    prompt: str
    conversation_id: Optional[int] = None
    model: str = Field(default=DEFAULT_IMAGE_MODEL)
    aspect_ratio: str = Field(default="1:1")   # 1:1 | 3:4 | 4:3 | 16:9 | 9:16
    n: int = Field(default=1, ge=1, le=5)       # 单次生成数量


class ImageGenerateResponse(BaseModel):
    artifact_id: int
    urls: list[str]
    prompt: str
    model: str
    aspect_ratio: str


class VideoGenerateRequest(BaseModel):
    prompt: str
    conversation_id: Optional[int] = None
    # 默认：字节 Seedance 2.0 Fast（抖音同款）
    model: str = Field(default="doubao-seedance-2-0-fast-260128")
    duration: int = Field(default=5, ge=5, le=15)  # 秒
    aspect_ratio: str = Field(default="16:9")
    generate_audio: bool = Field(default=True)  # 是否生成音频（Seedance 2.0 支持）


class VideoTaskResponse(BaseModel):
    task_id: str
    status: str  # pending | processing | done | failed
    progress: int  # 0-100
    video_url: Optional[str] = None
    artifact_id: Optional[int] = None
    error: Optional[str] = None


# ══════════════════════════════════════════════════════════
#  意图识别 · Haiku 兜底（前端关键词优先）
# ══════════════════════════════════════════════════════════

_anthropic_client: Optional[anthropic.AsyncAnthropic] = None
_anthropic_signature: Optional[tuple[str, str]] = None


def _get_anthropic():
    global _anthropic_client, _anthropic_signature
    key = get_app_setting("ANTHROPIC_API_KEY", "") or ""
    base_url = get_app_setting("ANTHROPIC_BASE_URL", "") or ""
    signature = (key, base_url)
    if _anthropic_client is None or _anthropic_signature != signature:
        if not key:
            raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
        import httpx
        kwargs = {
            "api_key": key,
            "timeout": httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        }
        if base_url:
            kwargs["base_url"] = base_url
        _anthropic_client = anthropic.AsyncAnthropic(**kwargs)
        _anthropic_signature = signature
    return _anthropic_client


INTENT_SYSTEM_PROMPT = """你是一个"用户意图识别器"。用户在对话框里敲了一段话，你要判断他到底想干什么。

只有 3 种意图：
- "image"：想生成/画一张图片
- "video"：想生成/制作一段视频
- "chat"：普通对话（包括问问题、咨询建议、讨论设计、聊天）

关键判定规则：
- "帮我画/生成/来张/出一张/做张" + 图/图片/画/插图/海报/封面 → image
- "帮我做/生成/出一个" + 视频/短视频/MV/片子 → video
- 只是咨询"XX 怎么设计/画"、"XX 看起来像什么"、"怎么做视频" → chat（是在问方法）
- "画龙点睛是什么意思" 这种包含"画"字但是问成语的 → chat
- "做个 logo" → 模糊，默认 chat（让用户再明确）
- 含糊其辞（比如只说了"小红书封面"）→ chat

输出严格 JSON：
{
  "intent": "image" | "video" | "chat",
  "confidence": 0.0-1.0,
  "prompt": "如果是 image/video，这里写从用户话里抽出的核心画面/主题描述（去掉'帮我画'这些指令词，只留画面描述）",
  "reason": "一句话说明判断理由"
}

只输出 JSON，不要其它文字。"""


@router.post("/intent-check", response_model=IntentCheckResponse)
async def intent_check(
    body: IntentCheckRequest,
    current_user: User = Depends(get_current_user),
):
    """
    用 Haiku 判断用户意图（关键词匹配后的兜底层）。
    前端应该先做关键词匹配；关键词没命中才调这个接口。
    """
    client = _get_anthropic()

    messages = []
    # 带上最近几条历史帮助判断（可选）
    if body.recent_history:
        # 只保留文字，最多 3 条
        recent = body.recent_history[-3:]
        ctx = "\n".join(f"[{m.get('role')}] {m.get('content', '')[:200]}" for m in recent)
        messages.append({"role": "user", "content": f"最近对话（上下文，不是要判断的内容）：\n{ctx}\n\n---\n\n现在要判断这句话：{body.text}"})
    else:
        messages.append({"role": "user", "content": body.text})

    try:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=INTENT_SYSTEM_PROMPT,
            messages=messages,
        )
        raw = resp.content[0].text if resp.content else ""
        # 抽 JSON
        import re
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return IntentCheckResponse(intent="chat", confidence=0.0, reason="Haiku 未返回 JSON")
        data = json.loads(m.group(0))
        return IntentCheckResponse(
            intent=data.get("intent", "chat"),
            confidence=float(data.get("confidence", 0.5)),
            prompt=data.get("prompt"),
            reason=data.get("reason"),
        )
    except Exception as e:
        # 任何异常都当普通聊天，不影响主流程
        return IntentCheckResponse(intent="chat", confidence=0.0, reason=f"error: {e}")


# ══════════════════════════════════════════════════════════
#  Provider 层：图片生成（真结构假实现）
# ══════════════════════════════════════════════════════════

ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "3:4": "1024x1365",
    "4:3": "1365x1024",
    "16:9": "1365x768",
    "9:16": "768x1365",
}


def _sanitize_image_prompt(prompt: str) -> str:
    """清理用户追问里的操作性口语，降低 ePhone invalid argument / sensitive 误判。"""
    text = " ".join(str(prompt or "").split()).strip()
    replacements = [
        (r"你直接生成[一二两三四五\d]*张?吧?", ""),
        (r"直接生成[一二两三四五\d]*张?吧?", ""),
        (r"一起生成(给我)?", ""),
        (r"生成给我", ""),
        (r"我选择一下", ""),
        (r"我挑(一)?下", ""),
        (r"图呢\??", ""),
        (r"图片呢\??", ""),
        (r"哪呢\??", ""),
        (r"[一二两三四五\d]+\s*张", ""),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = text.replace("赛博朋克", "霓虹未来科技")
    text = re.sub(r"年轻女生|年轻女孩|女生", "成年女性", text)
    text = re.sub(r"年轻男生|年轻男孩|男生", "成年男性", text)
    text = re.sub(r"[，,、\s]+$", "", text).strip(" ，,。")
    return text or str(prompt or "").strip()


def _safer_image_prompt(prompt: str) -> str:
    base = _sanitize_image_prompt(prompt)
    base = base.replace("深夜", "夜晚").replace("焦虑", "专注").replace("焦急", "专注")
    return (
        f"{base}。画面为成年人物，商业插画/摄影风格，健康安全，"
        "无血腥、无暴力、无色情、无未成年人。"
    )


def _is_retryable_image_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(key in text for key in [
        "sensitive",
        "invalid argument",
        "prediction failed",
        "modelerror",
        "flagged",
    ])


async def _ephone_poll_task(
    task_id: str,
    api_key: str,
    base_url: str,
    timeout_s: int = DEFAULT_IMAGE_TASK_TIMEOUT_SECONDS,
) -> dict:
    """轮询 ePhone 异步任务 · 每 2 秒查一次，最多 timeout_s 秒。"""
    import httpx
    url = f"{base_url}/v1/task/{task_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(2)
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"ePhone poll failed: HTTP {resp.status_code} {resp.text[:300]}")
            data = resp.json()
            status = (data.get("status") or "").lower()
            # 终态：completed（ePhone 实际返回值） / succeeded / success / done
            if status in ("completed", "succeeded", "success", "done"):
                return data
            if status in ("failed", "error", "canceled", "cancelled"):
                raise RuntimeError(f"ePhone task failed: {data.get('error') or data}")
    raise RuntimeError(f"ePhone task timeout after {timeout_s}s")


def _extract_image_urls_from_ephone_task(task_data: dict) -> list[str]:
    """从 ePhone 异步任务结果里提取图片 URL。

    ePhone 真实返回格式（经 probe 验证）：
      {"status":"completed","outputs":["https://storage.fonedis.cc/xxx.png"]}

    同时兼容其他可能的字段名以增强鲁棒性。
    """
    urls: list[str] = []

    # ─── 主路径：outputs 数组（ePhone 实际字段）──────────
    outs = task_data.get("outputs")
    if isinstance(outs, list):
        for item in outs:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                urls.append(item)
            elif isinstance(item, dict):
                u = item.get("url") or item.get("image_url") or item.get("b64_json")
                if u:
                    urls.append(u)
    if urls:
        return urls

    # ─── 兜底：尝试其他可能的字段结构 ──────────────
    output = task_data.get("output") or task_data.get("result") or {}
    imgs = output.get("images") if isinstance(output, dict) else None
    if isinstance(imgs, list):
        for item in imgs:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                u = item.get("url") or item.get("image_url") or item.get("b64_json")
                if u:
                    urls.append(u)
    if not urls and isinstance(output, dict) and output.get("url"):
        urls.append(output["url"])
    if not urls and isinstance(task_data.get("data"), list):
        for item in task_data["data"]:
            if isinstance(item, dict):
                u = item.get("url") or item.get("b64_json")
                if u:
                    urls.append(u)
    if not urls and isinstance(task_data.get("images"), list):
        urls.extend([x for x in task_data["images"] if isinstance(x, str)])
    return urls


async def _gen_image_ephone(prompt: str, aspect_ratio: str, n: int, model: str) -> list[str]:
    """
    通过 ePhone 调用图片生成（异步 /v1/task/submit 模式）。
    支持模型：
      - gpt-image-2                  (默认)
      - gemini-2.5-flash-image       (Nano Banana，¥0.042/次)
      - gemini-3-pro-image-preview   (Nano Banana Pro，高质量)
      - imagen-4.0-fast-generate-001 (Imagen Fast)
    """
    import httpx
    api_key = (get_app_setting("EPHONE_API_KEY", "") or "").strip()
    base_url = (get_app_setting("EPHONE_BASE_URL", "https://api.ephone.ai") or "https://api.ephone.ai").rstrip("/")
    if not api_key:
        print(f"[IMAGE] ⚠️  EPHONE_API_KEY 未配置 → 返回错误占位 (prompt={prompt[:30]!r} model={model})")
        return _error_placeholder_images(n, reason=f"no-api-key-{model}")

    safe_prompt = _sanitize_image_prompt(prompt)
    print(f"[IMAGE] 🎨 调用 ePhone API (model={model}, prompt={safe_prompt[:40]!r})")

    # 比例提示词（ePhone task 模式不支持原生 aspect_ratio 参数，用自然语言暗示模型）
    aspect_hint = {
        "1:1": "方形构图",
        "3:4": "竖屏构图 3:4",
        "4:3": "横屏构图 4:3",
        "16:9": "横屏构图 16:9，适合公众号封面",
        "9:16": "竖屏构图 9:16，适合短视频封面",
    }.get(aspect_ratio, "")
    final_prompt = f"{safe_prompt}{('，' + aspect_hint) if aspect_hint else ''}"
    fallback_prompt = f"{_safer_image_prompt(prompt)}{('，' + aspect_hint) if aspect_hint else ''}"

    submit_url = f"{base_url}/v1/task/submit"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    urls_all: list[str] = []
    errors: list[str] = []
    target_count = max(1, min(5, int(n or 1)))
    try:
        poll_timeout_s = int(get_app_setting("IMAGE_TASK_TIMEOUT_SECONDS", str(DEFAULT_IMAGE_TASK_TIMEOUT_SECONDS)) or DEFAULT_IMAGE_TASK_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        poll_timeout_s = DEFAULT_IMAGE_TASK_TIMEOUT_SECONDS
    poll_timeout_s = max(120, min(600, poll_timeout_s))
    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx in range(target_count):
            prompt_variants = [final_prompt]
            if fallback_prompt != final_prompt:
                prompt_variants.append(fallback_prompt)
            last_err: Exception | None = None
            for variant_idx, prompt_variant in enumerate(prompt_variants, start=1):
                try:
                    body = {"model": model, "input": {"prompt": prompt_variant}}
                    print(f"[IMAGE] → POST {submit_url} item={idx+1}/{target_count} try={variant_idx}")
                    r = await client.post(submit_url, headers=headers, json=body)
                    if r.status_code != 200:
                        raise RuntimeError(f"ePhone submit failed: HTTP {r.status_code} {r.text[:300]}")
                    submit_data = r.json()
                    task_id = submit_data.get("id") or submit_data.get("task_id") or submit_data.get("taskId")
                    print(f"[IMAGE] ✓ submit OK, task_id={task_id}")
                    if not task_id:
                        raise RuntimeError(f"ePhone submit didn't return task_id: {submit_data}")
                    task_data = await _ephone_poll_task(task_id, api_key, base_url, timeout_s=poll_timeout_s)
                    urls = _extract_image_urls_from_ephone_task(task_data)
                    print(f"[IMAGE] ✓ poll done, got {len(urls)} url(s): {urls[:1]}")
                    if not urls:
                        import json as _json
                        print(f"[IMAGE] ⚠️  提取 URL 失败，完整 task JSON:\n{_json.dumps(task_data, ensure_ascii=False, indent=2)[:2000]}")
                        raise RuntimeError("ePhone task completed but returned no image URL")
                    urls_all.extend(urls[:1])
                    break
                except Exception as e:
                    last_err = e
                    print(f"[IMAGE] ⚠️ item={idx+1}/{target_count} try={variant_idx} failed: {_exception_detail(e)}")
                    if variant_idx == 1 and _is_retryable_image_error(e):
                        continue
                    break
            if last_err and len(urls_all) <= idx:
                errors.append(_exception_detail(last_err))

    # 提取到真图就返回；单张失败不拖垮整批。
    if urls_all:
        return urls_all
    detail = "; ".join(errors[-3:]) if errors else f"empty-{model}"
    print(f"[IMAGE] ❌ 全部子任务均未提取到 URL: {detail}")
    raise RuntimeError(detail)


def _placeholder_images(prompt: str, n: int, tag: str = "placeholder") -> list[str]:
    """开发期占位图（key 没配时）· 用 picsum 随机图."""
    seed_base = abs(hash(prompt)) % 1000
    return [
        f"https://picsum.photos/seed/{tag}-{seed_base}-{i}/1024/1024"
        for i in range(n)
    ]


def _error_placeholder_images(n: int, reason: str = "failed") -> list[str]:
    """失败占位图 · 显眼红色 SVG（data URL，不联网）。
    告诉用户这不是真图，让 bug 暴露出来而不是被 picsum 伪装掩盖。
    """
    import urllib.parse
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<rect width="1024" height="1024" fill="#1A1814"/>
<rect x="40" y="40" width="944" height="944" fill="none" stroke="#D4645C" stroke-width="4" stroke-dasharray="12 8"/>
<text x="512" y="460" font-family="-apple-system,Inter,sans-serif" font-size="72" fill="#D4645C" text-anchor="middle" font-weight="700">❌ 生成失败</text>
<text x="512" y="540" font-family="-apple-system,Inter,sans-serif" font-size="28" fill="#9A7A3D" text-anchor="middle">请查看后端终端日志</text>
<text x="512" y="590" font-family="Menlo,monospace" font-size="22" fill="#6A6560" text-anchor="middle">reason: {reason}</text>
</svg>'''
    data_url = "data:image/svg+xml;utf8," + urllib.parse.quote(svg)
    return [data_url for _ in range(n)]


# ══════════════════════════════════════════════════════════
#  图片生成 · 主接口
# ══════════════════════════════════════════════════════════

@router.post("/image", response_model=ImageGenerateResponse)
async def generate_image(
    body: ImageGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    生成图片。返回 urls 列表 + 入库的 Artifact ID。
    """
    # 权限校验：conversation_id 必须属于当前用户（如果有）
    if body.conversation_id:
        conv = db.query(Conversation).filter(
            Conversation.id == body.conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

    configured_default_model = (get_app_setting("IMAGE_MODEL", DEFAULT_IMAGE_MODEL) or DEFAULT_IMAGE_MODEL).strip()
    requested_model = (body.model or "default").strip()

    # 模型名兼容：前端可能传旧名字或简名，统一映射到当前后台默认模型
    MODEL_ALIAS = {
        # 旧名字（前端历史版本）
        "gpt-image-1": configured_default_model,
        "imagen-4.0-fast-generate-001": configured_default_model,
        # 别名
        "gemini-nano-banana": "gemini-2.5-flash-image",
        "nano-banana": "gemini-2.5-flash-image",
        "nano-banana-pro": "gemini-3-pro-image-preview",
        "imagen-fast": configured_default_model,
        # 默认
        "default": configured_default_model,
    }
    real_model = MODEL_ALIAS.get(requested_model, requested_model or configured_default_model)

    # 路由到 ePhone provider
    try:
        urls = await _gen_image_ephone(body.prompt, body.aspect_ratio, body.n, real_model)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {_exception_detail(e)}")

    if not urls:
        raise HTTPException(status_code=500, detail="No image generated")

    # 入库 Artifact
    is_placeholder = not (get_app_setting("EPHONE_API_KEY", "") or "").strip()
    artifact = Artifact(
        user_id=current_user.id,
        conversation_id=body.conversation_id,
        type="image",
        title=body.prompt[:100],
        content=urls[0],  # 主图 URL
        artifact_metadata={
            "prompt": body.prompt,
            "model": real_model,
            "aspect_ratio": body.aspect_ratio,
            "n": body.n,
            "urls": urls,
            "placeholder": is_placeholder,
        },
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    return ImageGenerateResponse(
        artifact_id=artifact.id,
        urls=urls,
        prompt=body.prompt,
        model=real_model,
        aspect_ratio=body.aspect_ratio,
    )


# ══════════════════════════════════════════════════════════
#  视频生成 · 异步 task（Veo 3 占位）
# ══════════════════════════════════════════════════════════

# 简单的内存 task 存储（生产环境应该放 Redis 或 DB）
_video_tasks: dict[str, dict] = {}

# =============================================================================
# 🎬 v24 · P1.5 视频成本拦截 (2026-04-26)
# =============================================================================
# 用于:tools/video_gen.py 在调用 generate_video 前先检查预算
# 阈值:可在 .env 改 DAILY_VIDEO_BUDGET_CNY 和 VIDEO_PRICE_PER_SECOND
# =============================================================================

_user_daily_video_cost: dict[tuple[int, str], float] = {}

DAILY_VIDEO_BUDGET_CNY = float(os.getenv("DAILY_VIDEO_BUDGET_CNY", "50"))
VIDEO_PRICE_PER_SECOND = float(os.getenv("VIDEO_PRICE_PER_SECOND", "1.0"))


def check_daily_video_budget(user_id: int, duration_s: int, db=None):
    """检查用户当天累计视频生成预算是否够 · 不够就拦截。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    key = (int(user_id or 0), today)
    used = _user_daily_video_cost.get(key, 0.0)
    cost = float(duration_s) * VIDEO_PRICE_PER_SECOND
    budget = DAILY_VIDEO_BUDGET_CNY

    if used + cost > budget:
        msg = f"daily budget exceeded · used={used:.1f} · need={cost:.1f} · budget={budget:.0f}"
        print(f"[VIDEO BUDGET] ⛔ user={user_id} {msg}", flush=True)
        return False, msg, used, budget

    _user_daily_video_cost[key] = used + cost
    print(f"[VIDEO BUDGET] ✓ user={user_id} 今日累计 ¥{used + cost:.1f} / ¥{budget:.0f} (这次 ¥{cost:.1f})", flush=True)
    return True, "ok", used + cost, budget



async def _run_video_task(task_id: str, body: VideoGenerateRequest, user_id: int):
    """
    后台任务 · 调火山 Seedance（抖音同款）生成视频 · 异步轮询模式。

    火山 API 文档：
      POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
      GET  https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}
    """
    import httpx
    task = _video_tasks[task_id]
    ark_key = (get_app_setting("ARK_API_KEY", "") or "").strip()
    ark_base = (get_app_setting("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

    # 没配 key → 走占位模式（保留开发体验）
    if not ark_key:
        print(f"[VIDEO] ⚠️  ARK_API_KEY 未配置 → 走占位视频 (prompt={body.prompt[:30]!r})")
        stages = [(10, "准备...", 1), (40, "渲染...", 3), (80, "合成...", 3), (100, "完成", 1)]
        for p, _, w in stages:
            await asyncio.sleep(w)
            task["progress"] = p
            task["status"] = "processing" if p < 100 else "done"
        video_url = "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4"
        task["video_url"] = video_url
        _save_video_artifact(task, body, user_id, video_url, placeholder=True)
        return

    # ─── 真实调用火山 Seedance ──────────────────────
    try:
        submit_url = f"{ark_base}/contents/generations/tasks"
        headers = {
            "Authorization": f"Bearer {ark_key}",
            "Content-Type": "application/json",
        }
        # ⚠️ 火山 Seedance 2.0 fast 不接受 duration 参数，时长融入 prompt
        duration_hint = f"，时长约 {body.duration} 秒" if body.duration else ""
        final_prompt = f"{body.prompt}{duration_hint}"
        submit_body = {
            "model": body.model,
            "content": [
                {"type": "text", "text": final_prompt},
            ],
            "ratio": body.aspect_ratio,
            "generate_audio": body.generate_audio,
            "watermark": False,
        }
        print(f"[VIDEO] 🎬 调用火山 Seedance (model={body.model}, ratio={body.aspect_ratio}, duration≈{body.duration}s in prompt)")
        task["progress"] = 5
        task["status"] = "processing"

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(submit_url, headers=headers, json=submit_body)
        if r.status_code != 200:
            print(f"[VIDEO] ❌ submit HTTP {r.status_code}: {r.text[:600]}")
            raise RuntimeError(f"Ark submit HTTP {r.status_code}: {r.text[:300]}")
        submit_data = r.json()
        ark_task_id = submit_data.get("id")
        print(f"[VIDEO] ✓ submit OK, ark_task_id={ark_task_id}")
        if not ark_task_id:
            raise RuntimeError(f"Ark submit no id: {submit_data}")

        task["progress"] = 15

        # 轮询：火山实际耗时约 30-120 秒，每 3 秒查一次
        poll_url = f"{ark_base}/contents/generations/tasks/{ark_task_id}"
        deadline = time.time() + 300  # 最多等 5 分钟
        async with httpx.AsyncClient(timeout=30.0) as client:
            while time.time() < deadline:
                await asyncio.sleep(3)
                pr = await client.get(poll_url, headers=headers)
                if pr.status_code != 200:
                    raise RuntimeError(f"Ark poll HTTP {pr.status_code}: {pr.text[:200]}")
                pdata = pr.json()
                status = (pdata.get("status") or "").lower()

                # 火山状态：queued / running / succeeded / failed / cancelled / expired
                if status == "queued":
                    task["progress"] = max(task["progress"], 20)
                elif status == "running":
                    task["progress"] = min(task["progress"] + 5, 85)
                elif status in ("succeeded", "success", "completed", "done"):
                    # 尝试从多个可能字段提取 video_url
                    video_url = None
                    content = pdata.get("content") or {}
                    if isinstance(content, dict):
                        video_url = content.get("video_url") or content.get("url")
                    # 兜底：直接在顶层找
                    if not video_url:
                        video_url = pdata.get("video_url") or pdata.get("url")
                    # 兜底：outputs 数组（类 ePhone 风格）
                    if not video_url:
                        outs = pdata.get("outputs")
                        if isinstance(outs, list) and outs:
                            first = outs[0]
                            if isinstance(first, str):
                                video_url = first
                            elif isinstance(first, dict):
                                video_url = first.get("video_url") or first.get("url")
                    print(f"[VIDEO] ✓ {status}, video_url={video_url[:80] if video_url else None}")
                    if not video_url:
                        import json as _json
                        print(f"[VIDEO] ⚠️  终态但未找到 video_url，完整 JSON:\n{_json.dumps(pdata, ensure_ascii=False, indent=2)[:2000]}")
                        raise RuntimeError(f"Ark {status} but no video_url: {pdata}")
                    task["progress"] = 100
                    task["status"] = "done"
                    task["video_url"] = video_url
                    _save_video_artifact(task, body, user_id, video_url, placeholder=False, ark_task_id=ark_task_id)
                    return
                elif status in ("failed", "cancelled", "expired"):
                    err = pdata.get("error") or {}
                    print(f"[VIDEO] ❌ task {status}: {err}")
                    raise RuntimeError(f"Ark task {status}: {err}")
                # 其他状态继续轮询

        raise RuntimeError("Ark task timeout after 5 minutes")

    except Exception as e:
        print(f"[VIDEO] ❌ 任务失败: {e}")
        task["status"] = "failed"
        task["error"] = str(e)


def _save_video_artifact(task: dict, body: "VideoGenerateRequest", user_id: int,
                         video_url: str, placeholder: bool = False, ark_task_id: Optional[str] = None):
    """把生成完的视频存成 Artifact。"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        artifact = Artifact(
            user_id=user_id,
            conversation_id=body.conversation_id,
            type="video",
            title=body.prompt[:100],
            content=video_url,
            artifact_metadata={
                "prompt": body.prompt,
                "model": body.model,
                "aspect_ratio": body.aspect_ratio,
                "duration": body.duration,
                "generate_audio": body.generate_audio,
                "local_task_id": task.get("task_id"),
                "ark_task_id": ark_task_id,
                "placeholder": placeholder,
            },
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        task["artifact_id"] = artifact.id
    finally:
        db.close()


@router.post("/video", response_model=VideoTaskResponse)
async def generate_video(
    body: VideoGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    提交视频生成任务。返回 task_id，前端需轮询 GET /media/video/task/{task_id}。
    """
    if body.conversation_id:
        conv = db.query(Conversation).filter(
            Conversation.id == body.conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

    task_id = str(uuid.uuid4())
    _video_tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "video_url": None,
        "artifact_id": None,
        "error": None,
        "user_id": current_user.id,
        "created_at": time.time(),
    }

    # 启动后台任务（不阻塞响应）
    asyncio.create_task(_run_video_task(task_id, body, current_user.id))

    return VideoTaskResponse(
        task_id=task_id,
        status="pending",
        progress=0,
    )


@router.get("/video/task/{task_id}", response_model=VideoTaskResponse)
async def get_video_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """轮询视频生成状态。"""
    task = _video_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("user_id") != current_user.id:
        raise HTTPException(status_code=403, detail="Not your task")

    return VideoTaskResponse(
        task_id=task_id,
        status=task["status"],
        progress=task["progress"],
        video_url=task.get("video_url"),
        artifact_id=task.get("artifact_id"),
        error=task.get("error"),
    )


# ══════════════════════════════════════════════════════════
#  Artifact 查询（Artifacts 右栏用）
# ══════════════════════════════════════════════════════════

class ArtifactBrief(BaseModel):
    id: int
    type: str
    title: Optional[str]
    content: Optional[str]
    conversation_id: Optional[int]
    artifact_metadata: Optional[dict]
    created_at: str


@router.get("/artifacts", response_model=list[ArtifactBrief])
async def list_artifacts(
    conversation_id: Optional[int] = None,
    type: Optional[str] = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    查询 Artifact 列表。
    - conversation_id 给定：只看该对话的
    - type='image'/'video'：过滤类型
    """
    q = db.query(Artifact).filter(Artifact.user_id == current_user.id)
    if conversation_id:
        q = q.filter(Artifact.conversation_id == conversation_id)
    if type:
        q = q.filter(Artifact.type == type)
    q = q.order_by(Artifact.created_at.desc()).limit(limit)

    return [
        ArtifactBrief(
            id=a.id,
            type=a.type,
            title=a.title,
            content=a.content,
            conversation_id=a.conversation_id,
            artifact_metadata=a.artifact_metadata,
            created_at=a.created_at.isoformat(),
        )
        for a in q.all()
    ]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactBrief)
async def get_artifact(
    artifact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    a = db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.user_id == current_user.id,
    ).first()
    if not a:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return ArtifactBrief(
        id=a.id,
        type=a.type,
        title=a.title,
        content=a.content,
        conversation_id=a.conversation_id,
        artifact_metadata=a.artifact_metadata,
        created_at=a.created_at.isoformat(),
    )


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(
    artifact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除单条 Artifact（只能删自己的）。"""
    a = db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.user_id == current_user.id,
    ).first()
    if not a:
        raise HTTPException(status_code=404, detail="Artifact not found")
    db.delete(a)
    db.commit()
    return {"ok": True, "deleted_id": artifact_id}
