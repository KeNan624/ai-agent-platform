from __future__ import annotations

import mimetypes
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app_config import get_app_setting


class CosUploadError(RuntimeError):
    """Raised when media cannot be validated or uploaded to COS."""


IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def _env_required(name: str) -> str:
    value = (get_app_setting(name, "") or "").strip()
    if not value:
        raise CosUploadError(
            "COS 未配置完整，请在后台管理端设置 "
            "COS_SECRET_ID / COS_SECRET_KEY / COS_REGION / COS_BUCKET"
        )
    return value


def ensure_cos_configured() -> None:
    for name in ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET"):
        _env_required(name)
    try:
        import qcloud_cos  # noqa: F401
    except ImportError as exc:
        raise CosUploadError("缺少 cos-python-sdk-v5，请先安装依赖") from exc


def _get_int_env(name: str, default: int, min_value: int = 1) -> int:
    raw = (get_app_setting(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(min_value, int(raw))
    except ValueError:
        return default


def _allowed_map(kind: str) -> dict[str, str]:
    if kind == "image":
        return IMAGE_TYPES
    if kind == "video":
        return VIDEO_TYPES
    raise CosUploadError("kind 只能是 image 或 video")


def _max_bytes(kind: str) -> int:
    if kind == "image":
        return _get_int_env("PRACTICE_IMAGE_MAX_MB", 10) * 1024 * 1024
    return _get_int_env("PRACTICE_VIDEO_MAX_MB", 20480) * 1024 * 1024


def _clean_ext(filename: str, content_type: str | None, kind: str) -> tuple[str, str]:
    allowed = _allowed_map(kind)
    ext = Path(filename or "").suffix.lower()
    guessed_type = (mimetypes.guess_type(filename or "")[0] or "").lower()
    content_type = (content_type or guessed_type or "").split(";")[0].strip().lower()

    if ext not in allowed:
        for candidate_ext, candidate_type in allowed.items():
            if content_type == candidate_type:
                ext = candidate_ext
                break

    if ext not in allowed:
        allowed_exts = "、".join(sorted(allowed))
        raise CosUploadError(f"不支持的{('图片' if kind == 'image' else '视频')}格式，仅支持 {allowed_exts}")

    canonical_type = allowed[ext]
    if content_type and content_type not in {canonical_type, "application/octet-stream"}:
        raise CosUploadError(f"文件类型不匹配：{content_type} 不能作为 {kind} 上传")

    return ext, canonical_type


def _cos_client():
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise CosUploadError("缺少 cos-python-sdk-v5，请先安装依赖") from exc

    secret_id = _env_required("COS_SECRET_ID")
    secret_key = _env_required("COS_SECRET_KEY")
    region = _env_required("COS_REGION")
    token = (get_app_setting("COS_TOKEN", "") or "").strip() or None
    scheme = (get_app_setting("COS_SCHEME", "https") or "https").strip() or "https"
    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token,
        Scheme=scheme,
    )
    return CosS3Client(config)


def _public_url(bucket: str, region: str, key: str) -> str:
    public_base = (get_app_setting("COS_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    encoded_key = quote(key, safe="/")
    if public_base:
        return f"{public_base}/{encoded_key}"
    return f"https://{bucket}.cos.{region}.myqcloud.com/{encoded_key}"


def _object_key(user_id: int, kind: str, ext: str) -> str:
    now = datetime.utcnow()
    return f"practice/{now:%Y/%m}/{int(user_id)}/{kind}/{uuid.uuid4().hex}{ext}"


def _upload_local_path(
    *,
    local_path: str,
    original_filename: str,
    kind: str,
    content_type: str,
    size: int,
    user_id: int,
    ext: str,
) -> dict:
    bucket = _env_required("COS_BUCKET")
    region = _env_required("COS_REGION")
    key = _object_key(user_id=user_id, kind=kind, ext=ext)
    client = _cos_client()
    client.upload_file(
        Bucket=bucket,
        Key=key,
        LocalFilePath=local_path,
        PartSize=10,
        MAXThread=5,
        EnableMD5=False,
        ContentType=content_type,
    )
    return {
        "ok": True,
        "url": _public_url(bucket, region, key),
        "key": key,
        "filename": original_filename,
        "kind": kind,
        "content_type": content_type,
        "size": size,
    }


def _write_stream_to_temp(stream: BinaryIO, max_size: int) -> tuple[str, int]:
    fd, tmp_path = tempfile.mkstemp(prefix="practice-upload-", suffix=".bin")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise CosUploadError(f"文件过大，最大允许 {max_size // 1024 // 1024}MB")
                out.write(chunk)
        if size <= 0:
            raise CosUploadError("文件内容为空")
        return tmp_path, size
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def upload_fastapi_file(file: UploadFile, *, kind: str, user_id: int) -> dict:
    ext, content_type = _clean_ext(file.filename or "", file.content_type, kind)
    ensure_cos_configured()
    max_size = _max_bytes(kind)
    fd, tmp_path = tempfile.mkstemp(prefix="practice-upload-", suffix=ext)
    os.close(fd)
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise CosUploadError(f"文件过大，最大允许 {max_size // 1024 // 1024}MB")
                out.write(chunk)
        if size <= 0:
            raise CosUploadError("文件内容为空")
        return await run_in_threadpool(
            _upload_local_path,
            local_path=tmp_path,
            original_filename=file.filename or f"upload{ext}",
            kind=kind,
            content_type=content_type,
            size=size,
            user_id=user_id,
            ext=ext,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def upload_bytes(
    data: bytes,
    *,
    filename: str,
    kind: str,
    user_id: int,
    content_type: str | None = None,
) -> dict:
    ext, canonical_type = _clean_ext(filename, content_type, kind)
    ensure_cos_configured()
    max_size = _max_bytes(kind)
    size = len(data)
    if size <= 0:
        raise CosUploadError("文件内容为空")
    if size > max_size:
        raise CosUploadError(f"文件过大，最大允许 {max_size // 1024 // 1024}MB")

    fd, tmp_path = tempfile.mkstemp(prefix="practice-upload-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        return await run_in_threadpool(
            _upload_local_path,
            local_path=tmp_path,
            original_filename=filename,
            kind=kind,
            content_type=canonical_type,
            size=size,
            user_id=user_id,
            ext=ext,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
