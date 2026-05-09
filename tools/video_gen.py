# v24 (2026-04-26) · P1.5 视频融入主对话
# - 异步任务模式:工具立刻返回 task_id · 不阻塞 AI 流式输出
# - 前端拿到 task_id 自己轮询 GET /media/video/task/{task_id}
# - 默认时长 5 秒(便宜) · AI 看 prompt 决定 5/10/15 秒
# - 后端 _run_video_task 异步处理 · 我们这层不等
"""
主对话视频工具 · 跟 image_gen 不同:
  - 视频后端是真异步(asyncio.create_task) · 我们直接拿 task_id 返回
  - 不在这层等待 · 让前端轮询
  - 成本拦截在 routers/media.py 的 generate_video 入口做(每天 ¥50)

调用链:
  AI → tool_use generate_video(prompt, duration)
   → core.py _run_tool 分支
     → video_gen.generate(prompt, duration)
       → POST 内部调 routers.media.generate_video → 拿 task_id
         (后端 asyncio.create_task 跑后台任务 · 不等)
   → 立刻返回 {success: true, task_id, status: "pending", duration}
   → core.py emit video_started 事件 · 含 task_id
   → 前端开始轮询 + 显示进度气泡
"""
from typing import Optional


async def generate(prompt: str, duration: int = 5, aspect_ratio: str = "16:9", user_id: int = 0, db=None) -> dict:
    """
    主对话画视频工具 · 立刻返回 task_id · 不等待生成完成

    Args:
        prompt: 视频描述(中英文都可)
        duration: 时长 5/10/15 秒
        aspect_ratio: 比例 · 16:9 / 9:16 / 1:1
        user_id: 调用者(用来记录每日预算消耗)
        db: 数据库 session(用来检查每日预算)

    Returns:
        成功: {success: true, task_id: "...", status: "pending", duration, prompt}
        失败: {success: false, error: "...", prompt}
              · 失败原因可能是:超出每日预算 / 后端异常 / 参数无效
    """
    # 延迟导入 · 避免循环依赖
    from routers.media import (
        _video_tasks,
        _run_video_task,
        VideoGenerateRequest,
        check_daily_video_budget,
    )
    import asyncio
    import uuid
    import time

    # 校准 duration:后端限制 5-15 秒
    safe_duration = max(5, min(15, int(duration or 5)))

    # 校准 aspect_ratio
    if aspect_ratio not in ("16:9", "9:16", "1:1"):
        aspect_ratio = "16:9"

    # ─── 成本拦截:检查用户当天预算是否够 ───
    try:
        ok, msg, used_today, budget = check_daily_video_budget(user_id, safe_duration, db)
        if not ok:
            return {
                "success": False,
                "error": (
                    f"今日视频预算不够了:今天已经用了 ¥{used_today:.1f} · 这次需要 ¥{safe_duration:.0f} · "
                    f"超过每日预算 ¥{budget:.0f}。明天再试,或者去画图工坊画几张图。"
                ),
                "prompt": prompt,
                "budget_exceeded": True,
            }
    except Exception as e:
        # 成本检查失败不应该阻挡 · 打日志继续
        print(f"[VIDEO] ⚠️  预算检查异常(继续放行): {e}", flush=True)

    # ─── 直接复用 routers/media.py 的 _video_tasks + _run_video_task ───
    # 不调 HTTP 端点(避免 token 转发) · 直接构造 task 字典 + 起后台
    task_id = str(uuid.uuid4())
    body = VideoGenerateRequest(
        prompt=prompt,
        duration=safe_duration,
        aspect_ratio=aspect_ratio,
        # conversation_id 不传 · 主对话视频不绑会话(走 message 关联)
    )

    _video_tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "video_url": None,
        "artifact_id": None,
        "error": None,
        "user_id": user_id,
        "created_at": time.time(),
    }
    asyncio.create_task(_run_video_task(task_id, body, user_id))

    print(f"[🎬 v24] generate_video 已起 · task_id={task_id[:8]}... · duration={safe_duration}s · prompt={prompt[:30]!r}", flush=True)

    return {
        "success": True,
        "task_id": task_id,
        "status": "pending",
        "duration": safe_duration,
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "estimated_seconds_to_finish": 60 + safe_duration * 4,  # 给前端展示用 · 估算
    }


# Tool definition for Anthropic Tool Use API
TOOL_DEFINITION = {
    "name": "generate_video",
    "description": (
        "Generate a short video from a text prompt. Call this tool when the user asks you to "
        "create a video, make a movie clip, generate animation, or similar visual moving content. "
        "Common Chinese trigger phrases: 做个视频 / 生成视频 / 做一段动画 / 来段视频 / 做条短视频 / "
        "拍一段 / 录一段. "
        "\n\n"
        "DURATION SELECTION:\n"
        "Default to 5 seconds unless the user explicitly asks for longer. "
        "If user says '长一点' / '更长 / '10 秒' / '十秒' use 10. "
        "If user says '15 秒' / '十五秒' / '尽可能长' use 15. "
        "Hard maximum is 15 seconds (backend limit). "
        "If user asks for >15 seconds (e.g., '30 秒' / '1 分钟'), use 15 and tell user the current "
        "max is 15 seconds. "
        "\n\n"
        "ASPECT RATIO:\n"
        "Default 16:9 (landscape). Use 9:16 if user mentions 短视频 / 抖音 / 小红书 / 竖屏. "
        "1:1 only if user explicitly says 方形.\n"
        "\n"
        "IMPORTANT BEHAVIOR — VIDEO IS ASYNC:\n"
        "Unlike images, video generation takes 1-3 minutes. The tool returns IMMEDIATELY with a "
        "task_id (NOT the final video). The video will appear in chat when ready (about 1-3 min later).\n"
        "After calling this tool successfully:\n"
        "1. Tell the user the video is being generated, and approximately how long they should wait. "
        "Examples: '视频在生成了 · 大约 1-2 分钟出来 · 你可以先聊别的' / '收到 · 5 秒视频在路上 · 等我两分钟'\n"
        "2. DO NOT pretend the video is already done. DO NOT describe the video content as if you saw it.\n"
        "3. DO NOT paste the task_id to user.\n"
        "\n"
        "If success=false:\n"
        "1. Explain the failure reason briefly. The 'error' field has the user-friendly message — quote it.\n"
        "2. If budget_exceeded=true, tell user the daily budget hit and suggest tomorrow / image alternative.\n"
        "3. NEVER pretend the video was generated when success=false."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Video scene description. Be visual and concrete. "
                    "Mention key motion / camera movement / mood. "
                    "Chinese or English both work."
                ),
            },
            "duration": {
                "type": "integer",
                "enum": [5, 10, 15],
                "description": "Video length in seconds. Default 5. Max 15 (backend limit).",
                "default": 5,
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "description": "16:9 landscape (default), 9:16 vertical (短视频), 1:1 square.",
                "default": "16:9",
            },
        },
        "required": ["prompt"],
    },
}
