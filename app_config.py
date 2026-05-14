import os
from typing import Optional

from sqlalchemy import text

from database import SessionLocal


API_CONFIG_FIELDS = [
    {
        "key": "ANTHROPIC_API_KEY",
        "group": "对话模型",
        "label": "Anthropic API Key",
        "description": "用于主聊天、意图识别和 Claude 模型调用。",
        "secret": True,
        "default": "",
        "placeholder": "sk-ant-...",
    },
    {
        "key": "ANTHROPIC_BASE_URL",
        "group": "对话模型",
        "label": "Anthropic Base URL",
        "description": "兼容 Anthropic 协议的代理地址；留空使用官方默认地址。",
        "secret": False,
        "default": "",
        "placeholder": "https://api.anthropic.com",
    },
    {
        "key": "TAVILY_API_KEY",
        "group": "联网搜索",
        "label": "Tavily API Key",
        "description": "用于 internet_lookup 联网搜索工具。",
        "secret": True,
        "default": "",
        "placeholder": "tvly-...",
    },
    {
        "key": "SMSBAO_USERNAME",
        "group": "短信验证码",
        "label": "短信宝账号",
        "description": "短信宝平台账号，对应接口参数 u。",
        "secret": False,
        "default": "",
        "placeholder": "smsbao username",
    },
    {
        "key": "SMSBAO_API_KEY",
        "group": "短信验证码",
        "label": "短信宝 ApiKey",
        "description": "短信宝 ApiKey 或密码 MD5，对应接口参数 p；推荐使用 ApiKey。",
        "secret": True,
        "default": "",
        "placeholder": "ApiKey 或 32 位 MD5",
    },
    {
        "key": "SMSBAO_GOODS_ID",
        "group": "短信验证码",
        "label": "短信宝产品 ID",
        "description": "专用通道产品 ID，对应接口参数 g；通用短信产品可留空。",
        "secret": False,
        "default": "",
        "placeholder": "可选",
    },
    {
        "key": "SMS_CODE_TEMPLATE",
        "group": "短信验证码",
        "label": "验证码短信模板",
        "description": "必须和短信宝后台报备模板一致；可用 {code} 和 {minutes}。",
        "secret": False,
        "default": "【阿川AI】您的验证码是{code}，{minutes}分钟内有效。如非本人操作，请忽略。",
        "placeholder": "【签名】您的验证码是{code}，{minutes}分钟内有效。",
    },
    {
        "key": "SMS_DEV_MODE",
        "group": "短信验证码",
        "label": "短信开发模式",
        "description": "auto=未配置短信宝时只打印验证码；false=强制真实发送；true=总是只打印。",
        "secret": False,
        "default": "auto",
        "placeholder": "auto",
    },
    {
        "key": "SMS_CODE_EXPIRE_SECONDS",
        "group": "短信验证码",
        "label": "验证码有效期",
        "description": "单位秒，默认 300 秒。",
        "secret": False,
        "default": "300",
        "placeholder": "300",
    },
    {
        "key": "SMS_RESEND_INTERVAL_SECONDS",
        "group": "短信验证码",
        "label": "重复发送间隔",
        "description": "同一手机号再次获取验证码的最短间隔，单位秒。",
        "secret": False,
        "default": "60",
        "placeholder": "60",
    },
    {
        "key": "SMS_DAILY_LIMIT_PER_PHONE",
        "group": "短信验证码",
        "label": "单手机号日上限",
        "description": "同一手机号每天最多成功发送验证码次数。",
        "secret": False,
        "default": "10",
        "placeholder": "10",
    },
    {
        "key": "SMS_HOURLY_LIMIT_PER_IP",
        "group": "短信验证码",
        "label": "单 IP 小时上限",
        "description": "同一 IP 每小时最多成功发送验证码次数。",
        "secret": False,
        "default": "30",
        "placeholder": "30",
    },
    {
        "key": "SMS_MAX_VERIFY_ATTEMPTS",
        "group": "短信验证码",
        "label": "验证码尝试次数",
        "description": "同一验证码最多允许输错次数，超过后需要重新获取。",
        "secret": False,
        "default": "5",
        "placeholder": "5",
    },
    {
        "key": "SMS_REQUEST_TIMEOUT_SECONDS",
        "group": "短信验证码",
        "label": "短信宝请求超时",
        "description": "调用短信宝接口的 HTTP 超时时间，单位秒。",
        "secret": False,
        "default": "10",
        "placeholder": "10",
    },
    {
        "key": "EPHONE_API_KEY",
        "group": "图片生成",
        "label": "ePhone API Key",
        "description": "用于图片生成。",
        "secret": True,
        "default": "",
        "placeholder": "sk-...",
    },
    {
        "key": "EPHONE_BASE_URL",
        "group": "图片生成",
        "label": "ePhone Base URL",
        "description": "图片生成接口地址。",
        "secret": False,
        "default": "https://api.ephone.ai",
        "placeholder": "https://api.ephone.ai",
    },
    {
        "key": "IMAGE_MODEL",
        "group": "图片生成",
        "label": "图片默认模型",
        "description": "用于画图功能的默认模型；前端不展示给普通用户。",
        "secret": False,
        "default": "gpt-image-2",
        "placeholder": "gpt-image-2",
    },
    {
        "key": "IMAGE_TASK_TIMEOUT_SECONDS",
        "group": "图片生成",
        "label": "图片任务超时时间",
        "description": "等待外部图片生成任务完成的最长秒数；gpt-image-2 偶尔会超过 120 秒。",
        "secret": False,
        "default": "240",
        "placeholder": "240",
    },
    {
        "key": "ARK_API_KEY",
        "group": "视频生成",
        "label": "火山方舟 API Key",
        "description": "用于 Seedance 视频生成。",
        "secret": True,
        "default": "",
        "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    },
    {
        "key": "ARK_BASE_URL",
        "group": "视频生成",
        "label": "火山方舟 Base URL",
        "description": "视频生成接口地址。",
        "secret": False,
        "default": "https://ark.cn-beijing.volces.com/api/v3",
        "placeholder": "https://ark.cn-beijing.volces.com/api/v3",
    },
    {
        "key": "DOCMEE_API_KEY",
        "group": "PPT 生成",
        "label": "PPT 生成密钥",
        "description": "用于后台 PPT 一键生成功能，仅服务端保存和调用。",
        "secret": True,
        "default": "",
        "placeholder": "请输入 PPT 生成服务密钥",
    },
    {
        "key": "DOCMEE_BASE_URL",
        "group": "PPT 生成",
        "label": "PPT 生成服务地址",
        "description": "PPT 生成服务的接口地址，普通情况下无需修改。",
        "secret": False,
        "default": "",
        "placeholder": "默认服务地址",
    },
    {
        "key": "DOCMEE_TOKEN_HOURS",
        "group": "PPT 生成",
        "label": "临时凭证有效期",
        "description": "PPT 生成服务临时凭证有效期，单位小时。",
        "secret": False,
        "default": "2",
        "placeholder": "2",
    },
    {
        "key": "DOCMEE_TEMPLATE_SIZE",
        "group": "PPT 生成",
        "label": "随机模板数量",
        "description": "生成 PPT 时随机获取的模板数量，默认 1。",
        "secret": False,
        "default": "1",
        "placeholder": "1",
    },
    {
        "key": "COS_SECRET_ID",
        "group": "腾讯云 COS",
        "label": "SecretId",
        "description": "腾讯云访问密钥 SecretId，用于服务端上传课程封面、课时视频和实战区媒体。",
        "secret": True,
        "default": "",
        "placeholder": "AKID...",
    },
    {
        "key": "COS_SECRET_KEY",
        "group": "腾讯云 COS",
        "label": "SecretKey",
        "description": "腾讯云访问密钥 SecretKey，仅服务端保存和调用。",
        "secret": True,
        "default": "",
        "placeholder": "请输入 SecretKey",
    },
    {
        "key": "COS_REGION",
        "group": "腾讯云 COS",
        "label": "地域 Region",
        "description": "Bucket 所在地域，例如 ap-guangzhou、ap-shanghai。",
        "secret": False,
        "default": "ap-guangzhou",
        "placeholder": "ap-guangzhou",
    },
    {
        "key": "COS_BUCKET",
        "group": "腾讯云 COS",
        "label": "Bucket",
        "description": "Bucket 完整名称，例如 your-bucket-1250000000。",
        "secret": False,
        "default": "",
        "placeholder": "your-bucket-1250000000",
    },
    {
        "key": "COS_PUBLIC_BASE_URL",
        "group": "腾讯云 COS",
        "label": "访问域名",
        "description": "可填 CDN 或自定义域名；留空使用 COS 默认公网域名。",
        "secret": False,
        "default": "",
        "placeholder": "https://cdn.example.com",
    },
    {
        "key": "PRACTICE_IMAGE_MAX_MB",
        "group": "腾讯云 COS",
        "label": "图片大小上限 MB",
        "description": "实战区和课程后台图片上传大小限制。",
        "secret": False,
        "default": "10",
        "placeholder": "10",
    },
    {
        "key": "PRACTICE_VIDEO_MAX_MB",
        "group": "腾讯云 COS",
        "label": "视频大小上限 MB",
        "description": "实战区和课程后台视频上传大小限制。",
        "secret": False,
        "default": "20480",
        "placeholder": "20480",
    },
    {
        "key": "FEISHU_APP_ID",
        "group": "飞书文档",
        "label": "App ID",
        "description": "飞书企业自建应用 App ID，用于导入云文档。",
        "secret": False,
        "default": "",
        "placeholder": "cli_...",
    },
    {
        "key": "FEISHU_APP_SECRET",
        "group": "飞书文档",
        "label": "App Secret",
        "description": "飞书企业自建应用 App Secret，仅服务端保存和调用。",
        "secret": True,
        "default": "",
        "placeholder": "请输入 App Secret",
    },
]

API_CONFIG_BY_KEY = {field["key"]: field for field in API_CONFIG_FIELDS}


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read runtime config from DB first, then env, then the provided default."""
    try:
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT value FROM app_settings WHERE key = :key"),
                {"key": key},
            ).fetchone()
            if row and row[0] is not None:
                value = str(row[0]).strip()
                if value:
                    return value
    except Exception:
        pass

    env_value = os.getenv(key)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    return default


def get_api_config_items(db) -> list[dict]:
    rows = {
        row[0]: row[1]
        for row in db.execute(text("SELECT key, value FROM app_settings")).fetchall()
    }
    items = []
    for field in API_CONFIG_FIELDS:
        key = field["key"]
        db_value = rows.get(key)
        env_value = os.getenv(key)
        default_value = field.get("default") or ""

        if db_value is not None and str(db_value).strip():
            effective = str(db_value).strip()
            source = "database"
        elif env_value is not None and env_value.strip():
            effective = env_value.strip()
            source = "env"
        elif default_value:
            effective = default_value
            source = "default"
        else:
            effective = ""
            source = "empty"

        is_secret = bool(field["secret"])
        items.append({
            **field,
            "value": "" if is_secret else effective,
            "masked_value": _mask_secret(effective) if is_secret else "",
            "has_value": bool(effective),
            "source": source,
        })
    return items


def save_api_config(db, settings: dict[str, Optional[str]], clear_keys: Optional[list[str]] = None) -> list[str]:
    clear_set = set(clear_keys or [])
    changed: list[str] = []

    for key in clear_set:
        if key not in API_CONFIG_BY_KEY:
            continue
        db.execute(text("DELETE FROM app_settings WHERE key = :key"), {"key": key})
        changed.append(key)

    for key, raw_value in (settings or {}).items():
        field = API_CONFIG_BY_KEY.get(key)
        if not field:
            continue
        if key in clear_set:
            continue

        value = "" if raw_value is None else str(raw_value).strip()
        if field["secret"] and not value:
            continue
        if not field["secret"] and not value:
            db.execute(text("DELETE FROM app_settings WHERE key = :key"), {"key": key})
            changed.append(key)
            continue

        db.execute(text("""
            INSERT INTO app_settings (key, value, is_secret, updated_at)
            VALUES (:key, :value, :is_secret, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                is_secret = EXCLUDED.is_secret,
                updated_at = NOW()
        """), {"key": key, "value": value, "is_secret": bool(field["secret"])})
        changed.append(key)

    db.commit()
    return changed
