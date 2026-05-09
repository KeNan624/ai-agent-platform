# v23.1 (2026-04-26) · P1 工坊融入主对话 · bug 修复
# - 检测 ePhone 返回的 placeholder/error URL · 给 AI 返回 success=false 让它道歉
# - 加 1 次重试(总共试 2 次) · 抗 ePhone 偶发任务失败
# v23 (2026-04-26) · 新建 · 主对话画图工具
"""
主对话画图工具 · 跟 tools/search.py 同款结构。

调用链:
  AI → tool_use generate_image
   → core.py _run_tool 分支
     → image_gen.generate(prompt, aspect_ratio)
       → media._gen_image_ephone(...) [失败重试 1 次]
         → ePhone /v1/task/submit + poll
           → 返回 list[str] (图片 URL 列表 · 失败时是 placeholder URL)

返回给 AI 的结构(v23.1 改):
  成功: {"success": true, "urls": [...], "prompt": "...", "aspect_ratio": "..."}
  失败: {"success": false, "error": "...", "prompt": "..."}
       AI 看到 success=false 会停止"假装画好了" · 主动道歉 + 建议重试
"""
from typing import Optional


def _is_placeholder_or_error(urls: list[str]) -> bool:
    """检测 _gen_image_ephone 返回的 URL 是不是占位/错误图。
    真图 URL 长这样: https://storage.fonedis.cc/upload_xxx.png
    错误占位图 URL 长这样: 含 'placeholder' / 'error' / 'failed' / 'no-api-key' / 'empty-' 字样
    """
    if not urls:
        return True
    bad_keywords = ("placeholder", "error", "failed", "no-api-key", "empty-", "picsum.photos")
    for u in urls:
        if any(kw in (u or "").lower() for kw in bad_keywords):
            return True
    return False


async def generate(prompt: str, aspect_ratio: str = "1:1", n: int = 1) -> dict:
    """
    主对话里 AI 调用的画图函数 · 封装 media.py 的 _gen_image_ephone

    Args:
        prompt: 画图提示词(中英文都可)
        aspect_ratio: 比例 · 1:1 / 16:9 / 9:16 / 4:3 / 3:4
        n: 生成张数 · 1-5

    Returns:
        成功: {success: true, urls: [...], prompt: "...", aspect_ratio, model}
        失败: {success: false, error: "...", prompt: "..."}
    """
    # 延迟导入 · 避免循环依赖
    from app_config import get_app_setting
    from routers.media import DEFAULT_IMAGE_MODEL, _gen_image_ephone

    use_model = (get_app_setting("IMAGE_MODEL", DEFAULT_IMAGE_MODEL) or DEFAULT_IMAGE_MODEL).strip()
    safe_n = max(1, min(5, int(n or 1)))

    # v23.1 · 失败重试 1 次 · 抗 ePhone 偶发故障
    last_exception = None
    for attempt in range(2):
        try:
            urls = await _gen_image_ephone(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n=safe_n,
                model=use_model,
            )
            # 检测真图 vs 占位图
            if not _is_placeholder_or_error(urls):
                # ✅ 拿到真图
                return {
                    "success": True,
                    "urls": urls,
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "model": use_model,
                }
            # 占位图 · 第一次失败时记录 · 第二次还失败就退出
            print(f"[image_gen] ⚠️  attempt {attempt+1}/2 拿到占位图 · 准备重试", flush=True)
            last_exception = "ePhone task failed: returned placeholder URL"
        except Exception as e:
            print(f"[image_gen] ⚠️  attempt {attempt+1}/2 异常: {e}", flush=True)
            last_exception = str(e)

    # 两次都失败 · 给 AI 返回明确的失败信号
    return {
        "success": False,
        "error": (
            f"图片生成失败(已重试 1 次): {last_exception or '未知错误'}。"
            "请告诉用户暂时无法画图 · 建议稍后重试 · 或者去画图工坊试一下。"
        ),
        "prompt": prompt,
    }


# Tool definition for Anthropic Tool Use API
TOOL_DEFINITION = {
    "name": "generate_image",
    "description": (
        "Generate an image from a text prompt. Call this tool whenever the user asks you to "
        "draw, paint, create, generate, or design an image, picture, illustration, cover, or visual. "
        "Common Chinese trigger phrases include: 画 / 帮我画 / 生成图片 / 设计封面 / 给我画个 / "
        "来一张 / 出图 / 画一张 / 画个. "
        "Pick aspect_ratio based on the use case: "
        "1:1 (square, default for portraits and avatars), "
        "16:9 (landscape, ideal for 公众号封面 / wechat banner / blog header), "
        "9:16 (vertical, ideal for 短视频封面 / tiktok / xiaohongshu cover), "
        "4:3 (slight landscape), "
        "3:4 (slight portrait). "
        "\n\n"
        "IMPORTANT: After the tool returns, ALWAYS check the 'success' field in the result:\n"
        "- If success=true: briefly tell the user the image is ready (e.g. '画好啦~' / '这张你看看'). "
        "Do NOT repeat the prompt. Do NOT paste the URL. The image is automatically rendered.\n"
        "- If success=false: DO NOT pretend the image was generated. Apologize naturally, "
        "tell the user the generation failed, and suggest they retry or use the 画图工坊 instead. "
        "Quote the error briefly if it helps the user understand. "
        "NEVER say '画好啦' / 'here is your image' when success=false.\n"
        "\n"
        "If the user asks to refine ('改一下颜色' / '换个背景'), call this tool again with an updated prompt."
        "If the user asks for multiple images such as 五张 / 5张 / three options, set n accordingly "
        "and keep it between 1 and 5."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The image description. Be specific and visual. "
                    "Translate Chinese prompts to detailed visual descriptions when helpful, "
                    "but the model also accepts Chinese directly."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "description": "Image aspect ratio. Default 1:1 (square).",
                "default": "1:1",
            },
            "n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "How many images to generate. Use 1 by default, or the count requested by the user, capped at 5.",
                "default": 1,
            },
        },
        "required": ["prompt"],
    },
}
