# v27 (2026-04-29) · P2.B Mermaid + LaTeX 渲染(纯前端)
# - core.py 加 mermaid_latex_capability prompt · 教 AI 何时使用 mermaid / KaTeX 语法
# - 前端 enhanceMarkdown 钩子里调 mermaid.render + katex.renderToString
# - 不需要后端工具调用 · 纯前端渲染
#
# v26.1 (2026-04-29) · scraper 容错 + 标题智能提取
# - scraper.py 永远 return · 不 raise · 知乎反爬等失败时也能渲染卡片
# - 抓取标题加 og:title / h1 兜底 · 修微信"微信公众平台"垃圾标题
# - core.py emit url_scraped 时即使 result 异常 · 也用 tool_input.url 兜底
#
# v26 (2026-04-29) · P2.A URL 附件 · 主对话粘 URL 自动抓取
# - SIMPLE 分支 tools 列表加入 scraper.TOOL_DEFINITION (复用现有 Playwright 工具)
# - 加 url_scrape_capability prompt · AI 看到 URL 自动调 scrape_webpage
# - emit url_scraped 事件给前端 · 显示标题 + favicon 卡片
# - tool_result 含 url/title/content · 前端可挂气泡下方
#
# v24 (2026-04-26) · P1.5 视频融入主对话(异步轮询模式)
# - 新增 tools/video_gen.py · 主对话视频工具
# - SIMPLE 分支 tools 列表加入 video_gen.TOOL_DEFINITION
# - _run_tool 加 generate_video 分支(用户 user_id 通过 context 传)
# - 视频是异步任务 · 工具拿 task_id 立刻返回 · 不阻塞 · AI 收到 task_id 后告诉用户"在生成中"
# - emit video_started 事件给前端 · 含 task_id + duration · 前端开始轮询
# - 成本拦截在 video_gen.generate() + media.check_daily_video_budget() 双层
#
# v23.1 (2026-04-26) · P1 工坊融入主对话 · bug 修复
# - image_gen 检测占位图 · 返回 success=false 给 AI · 不再"假装画好了"
# - image_gen 加 1 次重试 · 抗 ePhone 偶发任务失败
# - core.py 只在 success=true 时 emit image_generated 事件 · 失败不渲染图卡
# - system prompt 强调 success 字段处理
#
# v23 (2026-04-26) · P1 工坊融入主对话
# - 新增 tools/image_gen.py · 主对话画图工具
# - SIMPLE 分支 tools 列表加入 image_gen.TOOL_DEFINITION
# - _run_tool 加 generate_image 分支
# - platform_capability_note 重写 · 不再说"用户去工坊点按钮" · 改成"你能直接画图"
# - 拿到图片 URL 后 yield image_generated 事件给前端渲染
#
# v22 (2026-04-26) · 修复 ePhone 中转拒绝 web_search 工具名问题:
# - 工具名从 web_search 全局改为 internet_lookup (含 tools/search.py + 本文件)
# - 同步修改 system_prompt 里所有 web_search 字样
# - 修复后引用卡片 / 中文回答 / tool_use 循环全部正常
# 详见 docs/04_当前进度与路线图.md v22 段
"""
Agent 主循环 · OpenClaw 式 4 阶段任务执行
==========================================

工作流程：
  1. PLAN      - 判断 + 规划（JSON）
     - 如果不是任务（闲聊/简单问答）→ 直接回复，结束
     - 如果是任务 → 输出 3-5 步计划，进入执行阶段
  2. EXECUTE   - 逐步执行，调用工具
  3. REFLECT   - 复盘执行结果，输出洞察和风险（JSON）
  4. RECOMMEND - 给出 2-4 个下一步选项（JSON）

事件流（async generator yield）：
  - 新增：plan_start / plan / step_start / step_done / reflect_start / reflection
          / recommend_start / recommend
  - 保留：text_delta / text / tool_call / tool_result / done / error
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncGenerator, Optional

import anthropic
import httpx

from app_config import get_app_setting
from agent.prompts import get_config, get_prompts
from tools import scraper as scraper_tool
from tools import search as search_tool
from tools import image_gen as image_gen_tool  # 🎨 v23 · P1 主对话画图工具
from tools import video_gen as video_gen_tool  # 🎬 v24 · P1.5 主对话视频工具

# ---------------------------------------------------------------------------
# Tool setup (unchanged from original)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    search_tool.TOOL_DEFINITION,
    scraper_tool.TOOL_DEFINITION,
]

TOOL_TIMEOUT = int(os.getenv("AGENT_TOOL_TIMEOUT", "30"))
MAX_TOKENS_DEFAULT = int(os.getenv("AGENT_MAX_TOKENS", "4096"))

# 🌐 v21 · 工具结果内存 LRU 缓存(10 分钟 TTL · 最多 64 条)
# 省钱 + 提速:同样的搜索 10 分钟内不重复请求 Tavily
import time
from collections import OrderedDict
_TOOL_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_TOOL_CACHE_TTL = 10 * 60   # 10 分钟
_TOOL_CACHE_MAX = 64

def _cache_key(name: str, inputs: dict) -> str:
    """稳定的 cache key · 忽略 inputs 里 key 的顺序"""
    return json.dumps({"_t": name, **(inputs or {})}, sort_keys=True, ensure_ascii=False)

def _cache_get(key: str) -> Optional[str]:
    now = time.time()
    if key not in _TOOL_CACHE:
        return None
    ts, value = _TOOL_CACHE[key]
    if now - ts > _TOOL_CACHE_TTL:
        _TOOL_CACHE.pop(key, None)
        return None
    # LRU: 命中后移到最后
    _TOOL_CACHE.move_to_end(key)
    return value

def _cache_put(key: str, value: str):
    _TOOL_CACHE[key] = (time.time(), value)
    _TOOL_CACHE.move_to_end(key)
    while len(_TOOL_CACHE) > _TOOL_CACHE_MAX:
        _TOOL_CACHE.popitem(last=False)


_client: Optional[anthropic.AsyncAnthropic] = None
_client_signature: Optional[tuple[str, str]] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client, _client_signature
    api_key = get_app_setting("ANTHROPIC_API_KEY", "") or ""
    base_url = get_app_setting("ANTHROPIC_BASE_URL", "") or ""
    signature = (api_key, base_url)
    if _client is None or _client_signature != signature:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        # Bug 1 修复：细分超时
        # - connect 10s：连不上 API 快速失败
        # - read 180s：流式读取给足时间（four_stage 四阶段累计可能较久）
        # - write / pool 10s：常规值
        kwargs = {
            "api_key": api_key,
            "timeout": httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0),
        }
        if base_url:
            kwargs["base_url"] = base_url
        _client = anthropic.AsyncAnthropic(
            **kwargs,
        )
        _client_signature = signature
    return _client


def _should_stream_model(model: Optional[str]) -> bool:
    """Only stream models that are known to match Anthropic SDK stream semantics."""
    model_id = (model or "").strip().lower()
    return model_id.startswith("claude-")


def _extract_urls(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    urls: list[str] = []
    for raw in re.findall(r"https?://[^\s<>'\"，。！？、；：）)】》]+", text):
        url = raw.rstrip(".,!?;:，。！？、；：）)]】》")
        if url and url not in urls:
            urls.append(url)
    return urls[:3]


def _has_unusable_compatible_tool_call(model: str, final_message: Any, tool_uses: list) -> bool:
    stop_reason = str(getattr(final_message, "stop_reason", "") or "").lower()
    return (
        not _should_stream_model(model)
        and not tool_uses
        and stop_reason in {"tool_call", "tool_use"}
    )


def _format_scraped_context(results: list[dict]) -> str:
    parts = [
        "系统已自动抓取用户消息中的网页。请基于下面的真实网页内容回答；"
        "如果某个网页抓取失败，请直接说明读不到，不要编造。"
    ]
    for idx, item in enumerate(results, start=1):
        url = item.get("url", "")
        title = item.get("title", "") or url
        error = item.get("error")
        content = (item.get("content", "") or "")[:6000]
        parts.append(
            f"<webpage index=\"{idx}\">\n"
            f"url: {url}\n"
            f"title: {title}\n"
            f"error: {error or ''}\n"
            f"content:\n{content}\n"
            f"</webpage>"
        )
    return "\n\n".join(parts)


def _build_url_scraped_payload(result_str: str, tool_input: dict) -> dict:
    try:
        result_obj = json.loads(result_str)
        if not isinstance(result_obj, dict):
            result_obj = {}
    except Exception:
        result_obj = {}

    url_val = result_obj.get("url", "") or tool_input.get("url", "")
    title_val = result_obj.get("title", "") or url_val
    content_val = result_obj.get("content", "") or ""
    return {
        "url": url_val,
        "title": title_val,
        "content_length": len(content_val),
        "error": result_obj.get("error"),
    }


async def _message_with_compatible_streaming(
    client: anthropic.AsyncAnthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    messages: list,
    tools: Optional[list] = None,
) -> AsyncGenerator[dict, None]:
    """Yield text deltas plus a final internal message event.

    ePhone's Anthropic-compatible channel can expose non-Claude models (for
    example deepseek-chat). Their non-streaming Messages API is compatible
    enough, but the Anthropic SDK's streaming snapshot parser may fail on
    content block indexes during tool calls. Keep true Claude models streaming
    and use create() for compatible non-Claude models.
    """
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools

    if _should_stream_model(model):
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield {"type": "text_delta", "content": delta.text}
            final_message = await stream.get_final_message()
    else:
        final_message = await client.messages.create(**kwargs)
        for block in final_message.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                yield {"type": "text_delta", "content": block.text}

    yield {"type": "_final_message", "content": final_message}


async def _run_tool(name: str, inputs: dict, context: Optional[dict] = None) -> str:
    """Dispatch a tool call and return its result as a JSON string.

    Args:
        name: 工具名
        inputs: 工具参数
        context: v24 · 给需要用户/db 的工具(目前是 generate_video) 用
                 {"user_id": int, "db": Session}
    """
    # 🌐 v21: 先查缓存(internet_lookup 查询语句相同 10 分钟内命中)
    # 🎨 v23: 画图不缓存(同样 prompt 用户可能想要不同的图)
    # 🎬 v24: 视频不缓存(同样 prompt 每次都重画 · 而且每次都要扣预算)
    cache_key = _cache_key(name, inputs)
    if name not in ("generate_image", "generate_video"):
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    try:
        if name == "internet_lookup":
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: search_tool.search(**inputs)
                ),
                timeout=TOOL_TIMEOUT,
            )
        elif name == "scrape_webpage":
            result = await asyncio.wait_for(
                scraper_tool.scrape(**inputs),
                timeout=TOOL_TIMEOUT,
            )
        elif name == "generate_image":
            # 🎨 v23 · P1 · 画图慢 · 单独 150 秒超时(ePhone 任务模式 · 含轮询)
            result = await asyncio.wait_for(
                image_gen_tool.generate(**inputs),
                timeout=150,
            )
        elif name == "generate_video":
            # 🎬 v24 · P1.5 · 视频立刻返回 task_id · 不阻塞 · 实际生成在后台
            # 给 video_gen.generate 注入 user_id / db 用于成本检查
            ctx = context or {}
            result = await asyncio.wait_for(
                video_gen_tool.generate(
                    **inputs,
                    user_id=ctx.get("user_id", 0),
                    db=ctx.get("db"),
                ),
                timeout=15,  # 提交任务 + 预算检查 · 应该 < 5 秒搞定
            )
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

        result_str = json.dumps(result, ensure_ascii=False)
        # 缓存:只缓存搜索/抓取
        if name not in ("generate_image", "generate_video"):
            _cache_put(cache_key, result_str)
        return result_str

    except asyncio.TimeoutError:
        return json.dumps({"error": f"Tool '{name}' timed out"})
    except Exception as e:
        return json.dumps({"error": f"Tool '{name}' failed: {str(e)}"})


# ---------------------------------------------------------------------------
# JSON extraction helper (robust against stray text around JSON)
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """
    Find the first top-level {...} JSON object in the text and parse it.
    Returns None if no valid JSON block found.
    """
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 🖼📄 阶段二 2.1/2.2: 附件 · 组装 user content(支持多模态 + 文档)
# ---------------------------------------------------------------------------

def _build_user_content(user_message: str, attachments: Optional[list] = None):
    """
    根据附件类型(图片 / 文档),构造 user 消息的 content 字段。

    无附件 → 返回字符串(兼容原有逻辑)
    有附件 → 返回 Anthropic 多模态 content array 或 带文档前缀的字符串

    attachments 格式:
        图片: {"kind":"image","media_type":"image/jpeg","data":"base64","name":"xxx.jpg"}
        文档: {"kind":"document","name":"xxx.pdf","extracted_text":"全文","meta":{...}}
        兼容 v18 旧格式: 无 kind 字段 → 按 image 处理
    """
    if not attachments:
        return user_message

    # 分类
    images = []
    documents = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        kind = att.get("kind", "image")  # 兼容旧格式默认 image
        if kind == "document":
            documents.append(att)
        else:
            images.append(att)

    # 组装文档前缀(纯文本,插到用户问题前)
    doc_prefix = ""
    if documents:
        parts = []
        for doc in documents:
            name = doc.get("name", "文档")
            text = doc.get("extracted_text", "")
            if not text:
                continue
            meta = doc.get("meta", {}) or {}
            fmt = meta.get("format", "?")
            pages = meta.get("pages")
            chars = meta.get("chars") or len(text)
            truncated = meta.get("truncated", False)
            # 文档头
            header_bits = [f"文件名: {name}", f"格式: {fmt}"]
            if pages:
                header_bits.append(f"页数: {pages}")
            header_bits.append(f"字符数: {chars}")
            if truncated:
                header_bits.append("⚠️ 内容已截断")
            parts.append(
                f"<attached_document>\n"
                f"{' · '.join(header_bits)}\n"
                f"---\n"
                f"{text}\n"
                f"</attached_document>"
            )
        if parts:
            doc_prefix = "\n\n".join(parts) + "\n\n---\n\n"

    # 用户文字部分(空时兜底)
    user_text_raw = user_message if user_message else (
        "请分析这份资料。" if documents else "请分析这张图。"
    )
    combined_text = doc_prefix + user_text_raw if doc_prefix else user_text_raw

    # 无图 → 纯字符串(让后端老链路不受影响)
    if not images:
        return combined_text

    # 有图 → 必须走 content array
    content_blocks = []
    for att in images:
        data = att.get("data")
        media_type = att.get("media_type", "image/jpeg")
        if not data:
            continue
        if data.startswith("data:"):
            data = data.split(",", 1)[-1]
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    content_blocks.append({"type": "text", "text": combined_text})
    return content_blocks


# ---------------------------------------------------------------------------
# Stage 1 · PLAN
# ---------------------------------------------------------------------------

async def _stage_plan(
    user_message: str,
    history: list,
    prompts: dict,
    config: dict,
) -> dict:
    """
    Call Claude to decide: is this a task? If so, output a plan.
    Returns:
        {"is_task": False, "reply": "..."}  OR
        {"is_task": True, "plan": {"title": "...", "steps": [...]}}
    """
    client = _get_client()
    # Only include the last few history turns to keep context small and avoid confusion
    recent_history = list(history)[-6:] if history else []
    messages = recent_history + [
        {"role": "user", "content": user_message},
        {"role": "user", "content": prompts["plan"]},
    ]
    resp = await client.messages.create(
        model=config["model_plan"],
        max_tokens=1024,
        system=prompts["system"],
        messages=messages,
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    parsed = _extract_json(text)
    if parsed is None:
        # JSON 解析失败 → 认定为"不是任务"，回退到对话模式
        # 不把 Claude 的原始回复（可能是防御性文字）当 reply，而是让执行阶段用对话模式重答
        return {"is_task": False, "reply": None, "_fallback": True}
    # 正常解析：确保结构完整
    if parsed.get("is_task") and "plan" not in parsed:
        return {"is_task": False, "reply": None, "_fallback": True}
    return parsed


# ---------------------------------------------------------------------------
# Stage 2 · EXECUTE (streaming, with tool use)
# ---------------------------------------------------------------------------

async def _stage_execute_step(
    user_message: str,
    step: dict,
    previous_results: list,
    prompts: dict,
    config: dict,
) -> AsyncGenerator[dict, None]:
    """
    Execute a single step. Streams text/tool events.
    Ends with an internal "_step_complete" marker containing the step summary.
    """
    client = _get_client()

    previous_summary = (
        "\n".join(
            f"- 步骤 {i+1}: {r.get('summary', '(无摘要)')}"
            for i, r in enumerate(previous_results)
        )
        if previous_results
        else "（这是第一步）"
    )
    step_instruction = prompts["execute"].format(
        step_index=step.get("index", "?"),
        step_title=step.get("title", ""),
        step_tool=step.get("tool", "reasoning"),
        query_hint=step.get("query_hint", ""),
        user_message=user_message,
        previous_results=previous_summary,
    )

    messages = [{"role": "user", "content": user_message}]
    for r in previous_results:
        if r.get("assistant_content"):
            messages.append({"role": "assistant", "content": r["assistant_content"]})
        if r.get("tool_results"):
            messages.append({"role": "user", "content": r["tool_results"]})
    messages.append({"role": "user", "content": step_instruction})

    step_text_parts: list[str] = []
    last_assistant_content: list = []
    last_tool_results: list = []

    max_inner_iterations = 3
    tools_enabled = True
    compat_tool_fallback_used = False
    for _iter in range(max_inner_iterations):
        final_message = None
        async for evt in _message_with_compatible_streaming(
            client,
            model=config["model_execute"],
            max_tokens=config.get("max_tokens", MAX_TOKENS_DEFAULT),
            system=prompts["system"],
            tools=TOOL_DEFINITIONS if tools_enabled else None,
            messages=messages,
        ):
            if evt["type"] == "_final_message":
                final_message = evt["content"]
            else:
                step_text_parts.append(evt["content"])
                yield evt

        if final_message is None:
            raise RuntimeError("模型没有返回结果")

        assistant_content = []
        for block in final_message.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                yield {
                    "type": "tool_call",
                    "content": {"name": block.name, "input": block.input},
                }
        messages.append({"role": "assistant", "content": assistant_content})
        last_assistant_content = assistant_content

        tool_uses = [b for b in final_message.content if getattr(b, "type", None) == "tool_use"]
        if _has_unusable_compatible_tool_call(config["model_execute"], final_message, tool_uses):
            if compat_tool_fallback_used:
                break
            compat_tool_fallback_used = True
            tools_enabled = False
            messages.append({
                "role": "user",
                "content": (
                    "系统提示：当前兼容模型没有返回可执行的工具参数。"
                    "请不要再调用工具，直接基于已有上下文完成这一步；"
                    "如果必须依赖实时联网或外部工具，请用中文说明当前模型暂时无法执行。"
                ),
            })
            continue

        if tool_uses:
            tool_results = []
            for block in tool_uses:
                result_str = await _run_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
                yield {
                    "type": "tool_result",
                    "content": {"name": block.name, "result": result_str},
                }
            messages.append({"role": "user", "content": tool_results})
            last_tool_results = tool_results
            continue

        break

    summary = "".join(step_text_parts)[:300]
    yield {
        "type": "_step_complete",
        "content": {
            "summary": summary,
            "assistant_content": last_assistant_content,
            "tool_results": last_tool_results,
        },
    }


# ---------------------------------------------------------------------------
# Stage 3 · REFLECT
# ---------------------------------------------------------------------------

async def _stage_reflect(
    user_message: str,
    all_step_results: list,
    prompts: dict,
    config: dict,
) -> dict:
    """Generate reflection JSON {insights: [...], risks: [...]}."""
    client = _get_client()
    summaries = "\n".join(
        f"步骤 {i+1}: {r.get('summary', '(无摘要)')}"
        for i, r in enumerate(all_step_results)
    )
    prompt = prompts["reflect"].format(
        user_message=user_message,
        all_step_results=summaries,
    )
    resp = await client.messages.create(
        model=config["model_reflect"],
        max_tokens=1024,
        system=prompts["system"],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    if parsed is None:
        return {"insights": ["（分析完成，但结果解析失败）"], "risks": []}
    return parsed


# ---------------------------------------------------------------------------
# Stage 4 · RECOMMEND
# ---------------------------------------------------------------------------

async def _stage_recommend(
    user_message: str,
    reflection: dict,
    prompts: dict,
    config: dict,
) -> dict:
    """Generate recommendation JSON {options: [...], question: "..."}."""
    client = _get_client()
    reflection_text = json.dumps(reflection, ensure_ascii=False, indent=2)
    prompt = prompts["recommend"].format(
        user_message=user_message,
        reflection=reflection_text,
    )
    resp = await client.messages.create(
        model=config["model_recommend"],
        max_tokens=1024,
        system=prompts["system"],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    if parsed is None:
        return {"options": [], "question": "你想继续聊聊这个话题吗？"}
    return parsed


# ---------------------------------------------------------------------------
# Conversational short-circuit (not a task → just stream a reply)
# ---------------------------------------------------------------------------

async def _stream_reply(
    user_message: str,
    history: list,
    prompts: dict,
    config: dict,
    attachments: Optional[list] = None,  # 🖼 阶段二 2.1
) -> AsyncGenerator[dict, None]:
    """Stream a conversational reply (used when is_task=False).
    🌐 v21: 闲聊分支也支持联网搜索(AI 自主决定)"""
    client = _get_client()
    user_content = _build_user_content(user_message, attachments)
    messages = list(history) + [{"role": "user", "content": user_content}]

    # 🌐 v21: 加入联网能力提示
    base_system = prompts.get("system", "你是一个有帮助的 AI 助手。")
    internet_lookup_hint = (
        "\n\n【联网搜索】如果用户问的是实时信息(新闻/价格/最新发布等),"
        "请调用 internet_lookup 工具搜索后再回答。闲聊和知识性问题不要搜。"
    )
    effective_system = base_system + internet_lookup_hint

    MAX_TOOL_ITERS = 5
    collected_citations: list[dict] = []
    tools_enabled = True
    compat_tool_fallback_used = False

    for iter_idx in range(MAX_TOOL_ITERS):
        final = None
        async for evt in _message_with_compatible_streaming(
            client,
            model=config["model_execute"],
            max_tokens=config.get("max_tokens", MAX_TOKENS_DEFAULT),
            system=effective_system,
            messages=messages,
            tools=[search_tool.TOOL_DEFINITION] if tools_enabled else None,
        ):
            if evt["type"] == "_final_message":
                final = evt["content"]
            else:
                yield evt

        if final is None:
            raise RuntimeError("模型没有返回结果")

        round_text = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")

        tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
        if _has_unusable_compatible_tool_call(config["model_execute"], final, tool_uses):
            if compat_tool_fallback_used:
                break
            compat_tool_fallback_used = True
            tools_enabled = False
            messages.append({
                "role": "user",
                "content": (
                    "系统提示：当前兼容模型没有返回可执行的联网工具参数。"
                    "请不要再调用工具，直接基于已有知识回答；"
                    "如果问题必须依赖实时信息，请用中文说明当前模型暂时无法联网获取。"
                ),
            })
            continue

        if not tool_uses:
            # 结束 · emit 最终文本
            if round_text:
                yield {"type": "text", "content": round_text}
            break

        # 执行工具
        tool_results_content = []
        for tu in tool_uses:
            yield {"type": "tool_call", "content": {"name": tu.name, "input": tu.input or {}}}
            result_str = await _run_tool(tu.name, tu.input or {})
            yield {"type": "tool_result", "content": {"name": tu.name, "result": result_str}}
            try:
                result_obj = json.loads(result_str)
                if isinstance(result_obj, dict) and isinstance(result_obj.get("results"), list):
                    for r in result_obj["results"]:
                        if isinstance(r, dict) and r.get("url"):
                            collected_citations.append({
                                "title": r.get("title", ""),
                                "url": r.get("url", ""),
                                "content": (r.get("content", "") or "")[:300],
                                "score": r.get("score", 0),
                                "query": (tu.input or {}).get("query", ""),
                            })
            except Exception:
                pass
            tool_results_content.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
            })

        messages.append({"role": "assistant", "content": [b.model_dump() for b in final.content]})
        messages.append({"role": "user", "content": tool_results_content})

    # emit 去重后的引用
    if collected_citations:
        seen_urls = set()
        unique = []
        for c in collected_citations:
            if c["url"] in seen_urls:
                continue
            seen_urls.add(c["url"])
            unique.append(c)
        yield {"type": "citations", "content": unique}


# ---------------------------------------------------------------------------
# MAIN ENTRY · run_agent
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    agent_type: str = "side_hustle",
    conversation_history: Optional[list] = None,
    model: Optional[str] = None,
    attachments: Optional[list] = None,  # 🖼 阶段二 2.1: 图片附件
) -> AsyncGenerator[dict, None]:
    """
    Main agent entry. Runs the 4-stage loop and streams events.

    Args:
        user_message: latest user message
        agent_type: which agent's prompts/config to use (e.g. "side_hustle")
        conversation_history: prior messages [{role, content}, ...]
        model: if set, overrides all stage models (legacy override)
        attachments: 图片附件 base64 列表 · 见 _build_user_content
    """
    try:
        prompts = get_prompts(agent_type)
        config = get_config(agent_type)
        if model:
            config["model_plan"] = model
            config["model_execute"] = model
            config["model_reflect"] = model
            config["model_recommend"] = model

        if not config.get("enabled", True):
            yield {"type": "error", "content": {"message": "该 Agent 当前已停用"}}
            yield {"type": "done", "content": None}
            return

        history = list(conversation_history or [])

        # ─────────── Stage 1 · PLAN ───────────
        yield {"type": "plan_start", "content": "正在理解你的任务..."}
        plan_result = await _stage_plan(user_message, history, prompts, config)

        # Not a task → just reply conversationally and stop
        if not plan_result.get("is_task"):
            reply = plan_result.get("reply")
            if reply:
                # Claude 已经给出了短回复
                yield {"type": "text", "content": reply}
            else:
                # 没有 reply（_fallback=True 或解析失败）→ 流式重新生成
                async for evt in _stream_reply(user_message, history, prompts, config, attachments=attachments):
                    yield evt
            yield {"type": "done", "content": None}
            return

        # Task path: emit plan card
        plan = plan_result.get("plan", {})
        yield {"type": "plan", "content": plan}

        # ─────────── Stage 2 · EXECUTE (loop over steps) ───────────
        all_step_results: list[dict] = []
        for step in plan.get("steps", []):
            yield {"type": "step_start", "content": step}

            step_complete_payload = None
            async for evt in _stage_execute_step(
                user_message, step, all_step_results, prompts, config
            ):
                if evt["type"] == "_step_complete":
                    step_complete_payload = evt["content"]
                else:
                    yield evt

            all_step_results.append(
                step_complete_payload
                or {"summary": "", "assistant_content": [], "tool_results": []}
            )
            yield {"type": "step_done", "content": {"index": step.get("index")}}

        # ─────────── Stage 3 · REFLECT ───────────
        yield {"type": "reflect_start", "content": "正在整理发现..."}
        reflection = await _stage_reflect(user_message, all_step_results, prompts, config)
        yield {"type": "reflection", "content": reflection}

        # ─────────── Stage 4 · RECOMMEND ───────────
        yield {"type": "recommend_start", "content": "正在给出建议..."}
        recommend = await _stage_recommend(user_message, reflection, prompts, config)
        yield {"type": "recommend", "content": recommend}

        yield {"type": "done", "content": None}

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "content": {"message": f"Agent 执行失败: {str(e)}"}}
        yield {"type": "done", "content": None}


# ═══════════════════════════════════════════════════════════════
#  New entry · Project-based agent
# ═══════════════════════════════════════════════════════════════

async def run_project_agent(
    user_message: str,
    project: Any = None,  # Project ORM instance or dict
    conversation_history: Optional[list] = None,
    attachments: Optional[list] = None,  # 🖼 阶段二 2.1: 图片附件
    user_id: int = 0,                    # 🎬 v24 · P1.5 视频成本拦截
    db: Any = None,                      # 🎬 v24 · P1.5 视频成本拦截
) -> AsyncGenerator[dict, None]:
    """
    根据 Project 配置决定走 simple 还是 four_stage 模式。

    Args:
        user_message: 用户最新消息
        project: Project ORM 实例 或 dict(包含 mode / system_prompt / model / four_stage_preset)
        conversation_history: 历史消息 [{role, content}, ...]
        attachments: 图片附件 base64 列表 · 见 _build_user_content
        user_id: 当前用户 ID(v24 · 给视频工具做成本拦截用)
        db: 数据库 session(v24 · 给视频工具做成本拦截用)
    """
    # Support both ORM instance and dict
    def _p(k, default=None):
        if project is None:
            return default
        if isinstance(project, dict):
            return project.get(k, default)
        return getattr(project, k, default)

    mode = _p("mode", "simple")
    model = _p("model", "claude-sonnet-4-6")
    system_prompt = _p("system_prompt") or ""

    # ────── FOUR_STAGE mode ──────
    if mode == "four_stage":
        preset = _p("four_stage_preset", "side_hustle")
        # 复用现有的 run_agent 入口
        async for evt in run_agent(
            user_message,
            agent_type=preset,
            conversation_history=conversation_history,
            model=model,
            attachments=attachments,  # 🖼 透传
        ):
            yield evt
        return

    # ────── SIMPLE mode ──────
    # 直接用 project.system_prompt 调用 Claude 流式返回
    try:
        client = _get_client()
        history = list(conversation_history or [])
        # 🖼 阶段二 2.1: 有图片时 content 变 array,无图时保持 string(兼容)
        user_content = _build_user_content(user_message, attachments)
        messages = history + [{"role": "user", "content": user_content}]
        tools_enabled = True
        compat_tool_fallback_used = False

        # ePhone's Anthropic-compatible adapter does not expose usable tool_use
        # blocks for some non-Claude models. URL scraping is deterministic enough
        # for the backend to handle before asking those models to summarize.
        if not _should_stream_model(model):
            scraped_results: list[dict] = []
            for url in _extract_urls(user_message):
                tool_input = {"url": url}
                yield {"type": "tool_call", "content": {"name": "scrape_webpage", "input": tool_input}}
                result_str = await _run_tool("scrape_webpage", tool_input, context={"user_id": user_id, "db": db})
                yield {"type": "tool_result", "content": {"name": "scrape_webpage", "result": result_str}}
                payload = _build_url_scraped_payload(result_str, tool_input)
                if payload.get("url"):
                    yield {"type": "url_scraped", "content": payload}
                try:
                    parsed = json.loads(result_str)
                    scraped_results.append(parsed if isinstance(parsed, dict) else payload)
                except Exception:
                    scraped_results.append(payload)

            if scraped_results:
                messages.append({"role": "user", "content": _format_scraped_context(scraped_results)})
                tools_enabled = False

        # 🎨 v23 / 🎬 v24 · 平台能力补丁(P1 + P1.5 工坊融入主对话):
        platform_capability_note = (
            "\n\n---\n"
            "【平台能力 · 重要】你运行在「阿川 AI 超级助手」平台上。"
            "你拥有以下工具能力(通过 tool_use 调用):\n"
            "1. internet_lookup · 联网搜索实时信息(详见下文【联网搜索能力】)\n"
            "2. generate_image · 直接生成图片(详见下文【画图能力】)\n"
            "3. generate_video · 直接生成视频(详见下文【视频能力】)\n"
            "4. scrape_webpage · 抓取任何 URL 的真实内容(详见下文【网页抓取能力】)\n"
            "\n"
            "用户体验提示:\n"
            "- 主对话(就是当前窗口)能搜索 + 画图 + 做视频 + 读网页 · 这是用户的主要入口\n"
            "- 平台还有独立的「画图工坊」「视频工坊」按钮供高级用户调更复杂的参数(更多模型 / 更长时长 / 风格选项等)"
        )

        # 🌐 v21 · 联网搜索能力补丁
        internet_lookup_capability = (
            "\n\n---\n"
            "【联网搜索能力】你可以调用 internet_lookup 工具搜索实时信息。\n"
            "什么时候必须搜索:\n"
            "- 用户问新闻/最新/今年/最近发生的事件\n"
            "- 用户问具体的产品价格、版本号、发售时间、股价、天气\n"
            "- 用户询问的信息你不确定(怕记错 · 宁可搜一下)\n"
            "- 用户明确说「帮我查」「搜一下」「上网看看」\n"
            "不要搜索:\n"
            "- 普通闲聊 / 问候 / 情感支持\n"
            "- 解释概念、原理、定义等知识性问题\n"
            "- 写作、代码、翻译等创造性任务\n"
            "- 用户明确说「不用联网」「不用搜」\n"
            "\n"
            "【搜索后回答的重要规则】\n"
            "1. **永远用中文回答**(除非用户明确用英文/其他语言提问)\n"
            "2. **直接给用户答案** · 不要复述「我将搜索...」「以下是搜索结果...」等过程性描述\n"
            "3. **不要输出 'I'll search for...' 'Here are the search results' 之类的英文套话**\n"
            "4. 不要粘贴原始搜索结果列表 · 把信息整理成清晰的结构(表格/要点/段落)\n"
            "5. 不要说「请注意这些是搜索结果,可能不准确」之类的免责声明 · 平台已经在下方展示引用来源\n"
            "6. 查到的英文内容要翻译成中文再呈现"
        )

        # 🎨 v23 · 画图能力补丁(P1 工坊融入主对话)
        image_gen_capability = (
            "\n\n---\n"
            "【画图能力】你可以调用 generate_image 工具直接在对话里出图。\n"
            "什么时候必须画图:\n"
            "- 用户说「画」「帮我画」「画一只」「画个」「来一张」「出图」「生成图片」「生成给我」「一起生成」\n"
            "- 用户说「设计封面」「公众号封面」「短视频封面」「头像」「插画」\n"
            "- 用户描述了一个具体的视觉场景且明显想要图(「一只戴墨镜的柴犬」)\n"
            "- 用户对刚才生成的图说「换个颜色」「换个背景」「再来一张」「改成 xxx 的」「五张都生成」「图呢」\n"
            "  → 把上一次的 prompt 加上修改要求 · 再调一次 generate_image\n"
            "- 用户要求多张图(如「五张」「5张」「来三版」) → generate_image 的 n 设成对应数量，最多 5\n"
            "\n"
            "不要画图:\n"
            "- 用户只是聊天提到「图」「照片」但没有生成需求(「这张图是哪儿拍的」)\n"
            "- 用户问「怎么画」/「怎么设计」这种知识性问题(应该回答方法论 · 不画图)\n"
            "- 用户上传了图让你看 / 描述 / 分析 · 不要回画一张新图\n"
            "\n"
            "【比例选择】根据使用场景选 aspect_ratio:\n"
            "- 默认 1:1(方形 · 通用)\n"
            "- 公众号封面 / 横屏 banner → 16:9\n"
            "- 短视频封面 / 小红书封面 / 抖音 → 9:16\n"
            "- 用户没说就用 1:1\n"
            "\n"
            "【画图后的回答规则 · 严格遵守】\n"
            "工具返回的 JSON 里有 success 字段 · 必须先看这个字段:\n"
            "- success=true (拿到真图):\n"
            "  1. 不要复述 prompt · 不要说「我将生成...」\n"
            "  2. 不要粘贴 URL · 图会自动渲染在你回答上方\n"
            "  3. 用一句简短的话告诉用户图好了 · 例如「画好啦~」「这张你看看」「按你说的画了一张」\n"
            "  4. 可以问一句要不要调整(「要不要换个背景?」)· 但不强求\n"
            "- success=false (生成失败):\n"
            "  1. **绝对不要说「画好啦」「这张你看看」** · 因为根本没有图\n"
            "  2. 自然地道歉 · 告诉用户这次失败了 · 例如「这次没画出来 · 不好意思」\n"
            "  3. **看 error 字段说一句通俗的原因** · 不要粘贴英文错误 · 但要让用户知道大概问题:\n"
            "     - error 含 'placeholder' / 'task failed' / 'no-api-key' → 「画图服务那边卡住了 · 我们重试过一次也没成 · 可能是高峰期」\n"
            "     - error 含 'timeout' → 「等图等了太久 · 这次超时了 · 网络可能慢」\n"
            "     - error 含 'budget' (视频才有) → 见视频能力规则\n"
            "     - 其它情况 → 「服务器那边出了点小问题 · 具体我也没拿到详细日志」\n"
            "  4. 建议用户重试 · 或者去「画图工坊」试试(「要不要再试一次? 或者去画图工坊调更精细的参数」)\n"
            "  5. 不要把详细的英文错误信息原样粘贴出来 · 但要让用户知道大致原因 · 不能只说『出问题了』就完事"
        )

        # 🎬 v24 · 视频能力补丁(P1.5 工坊融入主对话)
        video_gen_capability = (
            "\n\n---\n"
            "【视频能力】你可以调用 generate_video 工具在主对话里生成视频。\n"
            "什么时候调视频:\n"
            "- 用户说「做个视频」「生成视频」「来段视频」「做条短视频」「拍一段」「做个动画」\n"
            "- 用户描述了一个明显需要动态画面的场景(「猫从沙发跳下来」「海浪打在岸上」)\n"
            "- 用户对刚生成的视频说「换个场景」「再来一段」(就再调一次)\n"
            "\n"
            "什么时候不调视频:\n"
            "- 用户问「怎么做视频」「视频怎么剪」(知识性问题 · 不画)\n"
            "- 用户上传视频让你分析(还不支持)\n"
            "- 用户只是聊到「视频」但没有生成需求\n"
            "\n"
            "【时长选择】当前后端最长支持 15 秒:\n"
            "- 默认 5 秒(用户没明说时长)\n"
            "- 用户说「长一点」「久一点」「10 秒」→ 用 10\n"
            "- 用户说「最长」「15 秒」「拉满」→ 用 15\n"
            "- 用户说「30 秒」「1 分钟」等超过 15 秒的 → 用 15 + 主动告诉用户「主对话当前最长 15 秒 · 想更长可以去视频工坊」\n"
            "\n"
            "【比例选择】\n"
            "- 默认 16:9(横屏 · 通用)\n"
            "- 用户说「短视频」「抖音」「小红书」「竖屏」→ 9:16\n"
            "- 用户说「方形」→ 1:1\n"
            "\n"
            "【视频是异步任务 · 极重要】\n"
            "- 视频生成需要 1-3 分钟 · 不像图片那样几秒钟就完\n"
            "- 工具调用会**立刻返回 task_id 不等待生成完成** · 这是设计如此 · 不是 bug\n"
            "- 工具返回 success=true 时 · 视频还**没**真正生成完 · 只是任务已提交 · 你必须告诉用户「视频在生成 · 大约 1-3 分钟」\n"
            "- 视频完成时会自动出现在对话里(前端在轮询) · 不需要你做任何事\n"
            "\n"
            "【视频后的回答规则 · 严格遵守】\n"
            "工具返回的 JSON 里有 success 字段 · 必须先看这个字段:\n"
            "- success=true (任务已提交):\n"
            "  1. **不要复述 prompt** · 不要说「我将生成 xxx」\n"
            "  2. **不要假装视频已经做好了** · 视频还要 1-3 分钟才出 · 千万不要说「这个视频...」「画面里...」(你根本没看到)\n"
            "  3. 用一句简短的话告诉用户视频在生成 · 例如:\n"
            "     - 「视频在做了 · 大概 1-2 分钟出来 · 你可以先聊别的」\n"
            "     - 「收到 · 5 秒视频在路上 · 等我两分钟」\n"
            "     - 「视频任务已开始 · 完成时会自动出现在这里」\n"
            "  4. **绝对不要粘贴 task_id 给用户**\n"
            "- success=false (任务提交失败):\n"
            "  1. **绝对不要说「视频在做了」** · 因为根本没开始\n"
            "  2. 引用 error 字段里的提示告诉用户失败原因 · 例如「今天预算不够了 · 明天再试」\n"
            "  3. 如果是预算不够(budget_exceeded=true) · 建议用户:1) 明天再试 2) 改成画图(便宜很多)"
        )

        # 📎 v26 · P2.A · URL 抓取能力补丁
        url_scrape_capability = (
            "\n\n---\n"
            "【网页抓取能力】你可以调用 scrape_webpage 工具读取任何网页的真实内容。\n"
            "什么时候必须抓:\n"
            "- 用户消息里粘了 URL(http:// 或 https://)· 不论说「看看这个」「总结一下」「翻译一下」还是只粘 URL 没说话\n"
            "- 用户说「这个公众号文章讲什么」「这条知乎说啥」「这条微博」「这篇博客」并附了链接\n"
            "- internet_lookup 搜索结果的 snippet 太短不够答 · 需要读全文 · 也可以补一次 scrape_webpage\n"
            "\n"
            "什么时候不抓:\n"
            "- 用户没贴 URL · 别凭空抓\n"
            "- URL 看起来明显是图/视频文件(.jpg .png .mp4 等)· 抓不到正文\n"
            "- 用户说「不用打开」「我自己看就行」\n"
            "\n"
            "【抓取后的回答规则】\n"
            "1. **永远用中文回答** · 即使原文是英文 · 也要翻译成中文呈现\n"
            "2. **直接给用户精华** · 不要复述「我已经为你抓取了 xxx 网页 · 以下是内容...」\n"
            "3. 如果原文很长 · 总结成 3-5 个要点 · 用要点列表呈现\n"
            "4. 引用关键观点时可以原文摘录(简短几句即可)\n"
            "5. **绝对不要粘贴 URL 作为回答** · 用户已经知道 URL 是什么 · 平台会自动展示来源卡片\n"
            "6. 如果抓取失败(返回 error 字段)· 老老实实说「这个链接我读不到 · 可能是反爬或者需要登录」 · 不要瞎编内容"
        )

        # 📊 v27 · Mermaid + LaTeX 渲染能力
        mermaid_latex_capability = (
            "\n\n---\n"
            "【可视化能力 · 重要】平台前端会自动渲染以下两种特殊语法:\n"
            "\n"
            "1. **Mermaid 流程图**:用 ```mermaid 代码块写流程图 / 时序图 / 状态图等 · 平台会自动渲染成漂亮的 SVG 图\n"
            "   什么时候用:\n"
            "   - 用户问「画个流程图」「这个流程怎样」「展示一下架构」「类的关系」「时序」\n"
            "   - 涉及多步骤的过程 · 用文字说不清楚的(比如登录流程 / 数据流 / 状态机)\n"
            "   - 用户输入是描述步骤或决策树 · 你判断画图比文字更清楚\n"
            "   语法举例:\n"
            "   ```mermaid\n"
            "   flowchart TD\n"
            "     A[开始] --> B{是否登录?}\n"
            "     B -->|是| C[进首页]\n"
            "     B -->|否| D[去登录]\n"
            "   ```\n"
            "   注意:\n"
            "   - 节点中文用中括号 `[xxx]` 包起来 · 别用引号\n"
            "   - 避免节点 ID 用中文 · 用 A B C 这种英文字母 · 标签用中文\n"
            "   - 如果不确定语法 · 用最简单的 `flowchart TD` 或 `sequenceDiagram`\n"
            "\n"
            "2. **LaTeX 数学公式**:用 $$...$$ 写块级公式 · $...$ 写行内公式 · 平台会用 KaTeX 渲染\n"
            "   什么时候用:\n"
            "   - 用户问数学题 / 物理公式 / 统计公式\n"
            "   - 用户提到任何需要上下标、分数、根号、Σ、∫、矩阵等无法用纯文本好好表达的内容\n"
            "   语法举例:\n"
            "   - 行内:`圆面积是 $S = \\pi r^2$`\n"
            "   - 块级:`求根公式:$$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$`\n"
            "   注意:\n"
            "   - 如果只是讲价格(比如 \"$100\")· 不要用 LaTeX 语法 · 直接写 \"100 元\" 或加空格 \"$ 100\"\n"
            "   - 涉及希腊字母 / 上下标 / 分数时 · 一定要用 $...$ · 不要写 `pi r^2` 或 `π r²` 这种文本"
        )

        effective_system = (
            (system_prompt or "你是一个有帮助的 AI 助手。")
            + platform_capability_note
            + internet_lookup_capability
            + image_gen_capability
            + video_gen_capability
            + url_scrape_capability
            + mermaid_latex_capability
        )

        # 🌐 v21 · tool_use 多轮循环:Claude 可能多次搜索 / 画图 / 做视频
        # 🎨 v23 · 画图也走这个循环 · 同一对话里 AI 可能边搜边画
        # 🎬 v24 · 视频也走这个循环 · 但视频是异步 · AI 拿到 task_id 就跳出了
        MAX_TOOL_ITERS = 5
        full_text_parts: list[str] = []
        collected_citations: list[dict] = []  # 所有搜索累积的引用
        # 🎨 v23 · 累积所有画图结果(可能一次对话里画多张) · 之后 emit 给前端
        # 注意 · image_generated 事件在拿到结果时 *立刻* emit · 这里只是兜底记录
        # collected_images_meta = []  # 暂时不用 · 但留个口子

        for iter_idx in range(MAX_TOOL_ITERS):
            final = None
            async for evt in _message_with_compatible_streaming(
                client,
                model=model,
                max_tokens=MAX_TOKENS_DEFAULT,
                system=effective_system,
                messages=messages,
                tools=[
                    search_tool.TOOL_DEFINITION,
                    image_gen_tool.TOOL_DEFINITION,  # 🎨 v23
                    video_gen_tool.TOOL_DEFINITION,  # 🎬 v24 · P1.5
                    scraper_tool.TOOL_DEFINITION,    # 📎 v26 · P2.A URL 附件
                ] if tools_enabled else None,
            ):
                if evt["type"] == "_final_message":
                    final = evt["content"]
                else:
                    yield evt

            if final is None:
                raise RuntimeError("模型没有返回结果")

            # 提取这一轮产生的文本
            round_text = "".join(
                b.text for b in final.content if getattr(b, "type", None) == "text"
            )
            if round_text:
                full_text_parts.append(round_text)

            # 有 tool_use 吗?
            tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            print(f"[🌐 v21 SIMPLE 模式] 第 {iter_idx+1} 轮 · stop_reason={final.stop_reason} · tool_uses={len(tool_uses)}", flush=True)

            if _has_unusable_compatible_tool_call(model, final, tool_uses):
                if compat_tool_fallback_used:
                    break
                compat_tool_fallback_used = True
                tools_enabled = False
                messages.append({
                    "role": "user",
                    "content": (
                        "系统提示：当前兼容模型没有返回可执行的工具参数。"
                        "请不要再调用工具，直接基于已有上下文回答；"
                        "如果必须依赖联网、画图或视频工具，请用中文说明当前模型暂时无法执行该工具。"
                    ),
                })
                continue

            if not tool_uses:
                # 没有工具调用 · 完成
                break

            # 🌐 执行每个工具调用
            tool_results_content = []
            for tu in tool_uses:
                tool_name = tu.name
                tool_input = tu.input or {}
                # 告诉前端开始搜索/画图/做视频了
                yield {"type": "tool_call", "content": {"name": tool_name, "input": tool_input}}
                # 执行 · 视频工具需要 user_id / db (v24)
                tool_context = {"user_id": user_id, "db": db}
                result_str = await _run_tool(tool_name, tool_input, context=tool_context)
                # 告诉前端工具完成(前端可显示结果源/状态条)
                yield {"type": "tool_result", "content": {"name": tool_name, "result": result_str}}

                # 🌐 v21 · internet_lookup 累积引用
                if tool_name == "internet_lookup":
                    try:
                        result_obj = json.loads(result_str)
                        if isinstance(result_obj, dict) and isinstance(result_obj.get("results"), list):
                            for r in result_obj["results"]:
                                if isinstance(r, dict) and r.get("url"):
                                    collected_citations.append({
                                        "title": r.get("title", ""),
                                        "url": r.get("url", ""),
                                        "content": (r.get("content", "") or "")[:300],
                                        "score": r.get("score", 0),
                                        "query": tool_input.get("query", ""),
                                    })
                    except Exception:
                        pass

                # 🎨 v23 · generate_image 拿到图片 URL 立刻 emit 给前端渲染
                # 前端会把图直接插入到 AI 气泡里(类似 citations 卡片机制)
                # v23.1 · 只在 success=true 时 emit · 失败不要往前端推占位图
                elif tool_name == "generate_image":
                    try:
                        result_obj = json.loads(result_str)
                        if isinstance(result_obj, dict):
                            if result_obj.get("success") is True and isinstance(result_obj.get("urls"), list):
                                urls = result_obj["urls"]
                                prompt_text = result_obj.get("prompt", tool_input.get("prompt", ""))
                                aspect = result_obj.get("aspect_ratio", tool_input.get("aspect_ratio", "1:1"))
                                print(f"[🎨 v23.1 SIMPLE] emit image_generated: {len(urls)} 张 · prompt={prompt_text[:30]!r}", flush=True)
                                yield {
                                    "type": "image_generated",
                                    "content": {
                                        "urls": urls,
                                        "prompt": prompt_text,
                                        "aspect_ratio": aspect,
                                    },
                                }
                            else:
                                # 失败 · 不 emit · 让 AI 看到 success=false 道歉
                                err = result_obj.get("error", "unknown")
                                print(f"[🎨 v23.1 SIMPLE] generate_image 失败 · 不 emit 图卡 · err={err[:80]!r}", flush=True)
                    except Exception as e:
                        print(f"[🎨 v23.1 SIMPLE] image_generated emit 异常: {e}", flush=True)

                # 🎬 v24 · generate_video 拿到 task_id 立刻 emit · 让前端开始轮询
                # 视频是异步任务 · 这里只是提交成功 · 真实视频要 1-3 分钟后才出
                # 只在 success=true 时 emit · 失败让 AI 看到 success=false 道歉
                elif tool_name == "generate_video":
                    try:
                        result_obj = json.loads(result_str)
                        if isinstance(result_obj, dict):
                            if result_obj.get("success") is True and result_obj.get("task_id"):
                                task_id = result_obj["task_id"]
                                prompt_text = result_obj.get("prompt", tool_input.get("prompt", ""))
                                duration = result_obj.get("duration", 5)
                                aspect = result_obj.get("aspect_ratio", "16:9")
                                eta = result_obj.get("estimated_seconds_to_finish", 90)
                                print(f"[🎬 v24 SIMPLE] emit video_started: task_id={task_id[:8]}... · duration={duration}s · prompt={prompt_text[:30]!r}", flush=True)
                                yield {
                                    "type": "video_started",
                                    "content": {
                                        "task_id": task_id,
                                        "prompt": prompt_text,
                                        "duration": duration,
                                        "aspect_ratio": aspect,
                                        "estimated_seconds_to_finish": eta,
                                    },
                                }
                            else:
                                err = result_obj.get("error", "unknown")
                                print(f"[🎬 v24 SIMPLE] generate_video 失败 · 不 emit · err={err[:80]!r}", flush=True)
                    except Exception as e:
                        print(f"[🎬 v24 SIMPLE] video_started emit 异常: {e}", flush=True)

                # 📎 v26 · scrape_webpage 抓取成功 · emit url_scraped 让前端显示来源卡
                # v26.1 · 即使 result 没有 url 字段也兜底用 tool_input.url · 保证卡片永远渲染
                elif tool_name == "scrape_webpage":
                    try:
                        result_obj = json.loads(result_str)
                        if not isinstance(result_obj, dict):
                            result_obj = {}
                        # url 优先用结果的 · 兜底用工具入参的
                        url_val = result_obj.get("url", "") or tool_input.get("url", "")
                        title_val = result_obj.get("title", "") or url_val
                        content_val = result_obj.get("content", "") or ""
                        err_val = result_obj.get("error")
                        if url_val:
                            print(f"[📎 v26 SIMPLE] emit url_scraped: url={url_val[:60]!r} · title={title_val[:40]!r} · err={err_val!r}", flush=True)
                            yield {
                                "type": "url_scraped",
                                "content": {
                                    "url": url_val,
                                    "title": title_val,
                                    "content_length": len(content_val),
                                    "error": err_val,
                                },
                            }
                    except Exception as e:
                        # 工具结果 JSON parse 失败 · 还是 emit 一下含 input.url + error
                        print(f"[📎 v26 SIMPLE] url_scraped emit 异常: {e}", flush=True)
                        try:
                            url_fallback = tool_input.get("url", "") if isinstance(tool_input, dict) else ""
                            if url_fallback:
                                yield {
                                    "type": "url_scraped",
                                    "content": {
                                        "url": url_fallback,
                                        "title": "",
                                        "content_length": 0,
                                        "error": f"tool result parse failed: {e}",
                                    },
                                }
                        except Exception:
                            pass

                # 塞给 Claude 作为下一轮上下文
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                })

            # 继续下一轮 · 塞入 assistant 的 final.content + user 的 tool_results
            messages.append({"role": "assistant", "content": [b.model_dump() for b in final.content]})
            messages.append({"role": "user", "content": tool_results_content})

        # 🌐 如果有引用 · 去重并 emit 给前端(供卡片渲染)
        if collected_citations:
            seen_urls = set()
            unique = []
            for c in collected_citations:
                if c["url"] in seen_urls:
                    continue
                seen_urls.add(c["url"])
                unique.append(c)
            print(f"[🌐 v21 SIMPLE] emit citations: {len(unique)} 条", flush=True)
            yield {"type": "citations", "content": unique}
        else:
            print(f"[🌐 v21 SIMPLE] 本轮无 citations", flush=True)

        # Emit full text for saving
        full_text = "\n\n".join(p for p in full_text_parts if p)
        if full_text:
            yield {"type": "text", "content": full_text}
        yield {"type": "done", "content": None}

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "content": {"message": f"Agent 执行失败: {str(e)}"}}
        yield {"type": "done", "content": None}
