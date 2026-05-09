"""
Admin Courses Router · 课程管理后台
=====================================
管理员鉴权：手机号白名单
COS 上传：预留接口，配置后填 SecretId/SecretKey/Bucket
"""

from __future__ import annotations
from datetime import datetime
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user, is_default_admin_phone
from database import get_db
from models import PracticeCourse, PracticeLesson, User
from services.cos_upload import CosUploadError, upload_fastapi_file

router = APIRouter(prefix="/admin/courses", tags=["admin-courses"])
UPLOAD_STATUS: dict[str, dict] = {}

def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not getattr(current_user, "is_admin", False) and not is_default_admin_phone(current_user.phone):
        raise HTTPException(403, "无管理员权限")
    return current_user


def _clean_upload_id(upload_id: Optional[str]) -> Optional[str]:
    upload_id = (upload_id or "").strip()
    if not upload_id:
        return None
    return "".join(ch for ch in upload_id[:80] if ch.isalnum() or ch in "-_") or None


def _set_upload_status(upload_id: Optional[str], status: str, **payload) -> None:
    upload_id = _clean_upload_id(upload_id)
    if not upload_id:
        return
    now = time.time()
    for key, value in list(UPLOAD_STATUS.items()):
        if now - float(value.get("updated_at", 0)) > 3600:
            UPLOAD_STATUS.pop(key, None)
    UPLOAD_STATUS[upload_id] = {"status": status, "updated_at": now, **payload}


# ═══════════════════════════════════════════════
#  Schemas
# ═══════════════════════════════════════════════

class CourseCreate(BaseModel):
    slug: str
    title: str
    description: Optional[str] = None
    cover_url: Optional[str] = None
    cover_emoji: Optional[str] = None
    category: Optional[str] = None
    instructor: Optional[str] = None
    content_kind: str = "course"
    price: float = 0
    sort_order: int = 0
    is_published: bool = False


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    cover_emoji: Optional[str] = None
    category: Optional[str] = None
    instructor: Optional[str] = None
    content_kind: Optional[str] = None
    price: Optional[float] = None
    sort_order: Optional[int] = None
    status: Optional[str] = None
    is_published: Optional[bool] = None


class LessonCreate(BaseModel):
    title: str
    description: Optional[str] = None
    lesson_type: str = "video"
    video_url: Optional[str] = None
    video_duration: int = 0
    article_content: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    source_doc_id: Optional[str] = None
    sort_order: int = 0
    is_free: bool = False
    is_published: bool = True


class LessonUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    lesson_type: Optional[str] = None
    video_url: Optional[str] = None
    video_duration: Optional[int] = None
    article_content: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    source_doc_id: Optional[str] = None
    sort_order: Optional[int] = None
    is_free: Optional[bool] = None
    is_published: Optional[bool] = None


class ReorderItem(BaseModel):
    id: int
    sort_order: int


class ReorderRequest(BaseModel):
    items: list[ReorderItem] = Field(default_factory=list)
    lesson_ids: list[int] = Field(default_factory=list)


def _normalize_url(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _validate_lesson_media(lesson_type: str, video_url: Optional[str]) -> None:
    if lesson_type == "video" and not _normalize_url(video_url):
        raise HTTPException(400, "视频课必须先上传视频或填写视频链接")


# ═══════════════════════════════════════════════
#  课程 CRUD
# ═══════════════════════════════════════════════

@router.get("")
def admin_list_courses(
    content_kind: Optional[str] = Query("course"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    query = db.query(PracticeCourse)
    if content_kind:
        query = query.filter(PracticeCourse.content_kind == content_kind)
    courses = query.order_by(
        PracticeCourse.sort_order.asc(), PracticeCourse.id.asc()
    ).all()
    return [
        {
            "id": c.id, "slug": c.slug, "title": c.title, "description": c.description,
            "cover_url": c.cover_url, "cover_emoji": c.cover_emoji, "category": c.category,
            "instructor": c.instructor, "content_kind": c.content_kind or "course",
            "price": float(c.price or 0), "status": c.status,
            "sort_order": c.sort_order, "lesson_count": c.lesson_count,
            "total_duration": c.total_duration, "view_count": c.view_count,
            "is_published": c.is_published,
        }
        for c in courses
    ]


@router.post("")
def admin_create_course(body: CourseCreate, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    existing = db.query(PracticeCourse).filter(PracticeCourse.slug == body.slug).first()
    if existing:
        raise HTTPException(400, f"slug '{body.slug}' 已存在")
    c = PracticeCourse(
        slug=body.slug, title=body.title, description=body.description,
        cover_url=body.cover_url, cover_emoji=body.cover_emoji,
        category=body.category, instructor=body.instructor,
        content_kind=body.content_kind if body.content_kind in {"course", "column"} else "course",
        price=body.price, sort_order=body.sort_order,
        is_published=body.is_published,
        status="published" if body.is_published else "draft",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"ok": True, "id": c.id, "slug": c.slug}


@router.put("/{course_id}")
def admin_update_course(course_id: int, body: CourseUpdate, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    c = db.query(PracticeCourse).filter(PracticeCourse.id == course_id).first()
    if not c:
        raise HTTPException(404, "课程不存在")
    data = body.model_dump(exclude_none=True)
    if data.get("content_kind") not in {None, "course", "column"}:
        raise HTTPException(400, "content_kind 只能是 course 或 column")
    for field, val in data.items():
        setattr(c, field, val)
    if body.is_published is True:
        c.status = "published"
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/{course_id}")
def admin_delete_course(course_id: int, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    c = db.query(PracticeCourse).filter(PracticeCourse.id == course_id).first()
    if not c:
        raise HTTPException(404, "课程不存在")
    db.delete(c)
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════
#  课时 CRUD
# ═══════════════════════════════════════════════

@router.get("/{course_id}/lessons")
def admin_list_lessons(course_id: int, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    lessons = db.query(PracticeLesson).filter(
        PracticeLesson.course_id == course_id
    ).order_by(PracticeLesson.sort_order.asc(), PracticeLesson.id.asc()).all()
    return [
        {
            "id": l.id, "title": l.title, "description": l.description,
            "lesson_type": l.lesson_type, "video_url": l.video_url,
            "video_duration": l.video_duration, "article_content": l.article_content,
            "source_type": l.source_type, "source_url": l.source_url,
            "source_doc_id": l.source_doc_id, "sort_order": l.sort_order,
            "is_free": l.is_free, "is_published": l.is_published, "view_count": l.view_count,
        }
        for l in lessons
    ]


@router.post("/{course_id}/lessons")
def admin_create_lesson(course_id: int, body: LessonCreate, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    c = db.query(PracticeCourse).filter(PracticeCourse.id == course_id).first()
    if not c:
        raise HTTPException(404, "课程不存在")
    body.video_url = _normalize_url(body.video_url)
    _validate_lesson_media(body.lesson_type, body.video_url)
    l = PracticeLesson(
        course_id=course_id, title=body.title, description=body.description,
        lesson_type=body.lesson_type, video_url=body.video_url,
        video_duration=body.video_duration, article_content=body.article_content,
        source_type=body.source_type, source_url=body.source_url, source_doc_id=body.source_doc_id,
        sort_order=body.sort_order, is_free=body.is_free, is_published=body.is_published,
    )
    db.add(l)
    # 更新课程冗余字段
    c.lesson_count = (c.lesson_count or 0) + 1
    c.total_duration = (c.total_duration or 0) + (body.video_duration or 0)
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(l)
    return {"ok": True, "id": l.id}


@router.put("/lessons/{lesson_id}")
def admin_update_lesson(lesson_id: int, body: LessonUpdate, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    l = db.query(PracticeLesson).filter(PracticeLesson.id == lesson_id).first()
    if not l:
        raise HTTPException(404, "课时不存在")
    old_duration = l.video_duration or 0
    data = body.model_dump(exclude_none=True)
    if "video_url" in data:
        data["video_url"] = _normalize_url(data["video_url"])
    next_lesson_type = data.get("lesson_type", l.lesson_type)
    next_video_url = data.get("video_url", l.video_url)
    _validate_lesson_media(next_lesson_type, next_video_url)
    for field, val in data.items():
        setattr(l, field, val)
    l.updated_at = datetime.utcnow()
    # 同步课程总时长
    if body.video_duration is not None:
        c = db.query(PracticeCourse).filter(PracticeCourse.id == l.course_id).first()
        if c:
            c.total_duration = max(0, (c.total_duration or 0) - old_duration + (body.video_duration or 0))
            c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/lessons/{lesson_id}")
def admin_delete_lesson(lesson_id: int, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    l = db.query(PracticeLesson).filter(PracticeLesson.id == lesson_id).first()
    if not l:
        raise HTTPException(404, "课时不存在")
    c = db.query(PracticeCourse).filter(PracticeCourse.id == l.course_id).first()
    if c:
        c.lesson_count = max(0, (c.lesson_count or 0) - 1)
        c.total_duration = max(0, (c.total_duration or 0) - (l.video_duration or 0))
        c.updated_at = datetime.utcnow()
    db.delete(l)
    db.commit()
    return {"ok": True}


@router.put("/lessons/reorder")
def admin_reorder_lessons(body: ReorderRequest, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    if body.items:
        for item in body.items:
            l = db.query(PracticeLesson).filter(PracticeLesson.id == item.id).first()
            if l:
                l.sort_order = item.sort_order
    elif body.lesson_ids:
        for idx, lesson_id in enumerate(body.lesson_ids):
            l = db.query(PracticeLesson).filter(PracticeLesson.id == lesson_id).first()
            if l:
                l.sort_order = idx * 10
    else:
        raise HTTPException(400, "缺少排序数据")
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════
#  文件上传（COS）
# ═══════════════════════════════════════════════

@router.get("/upload-status/{upload_id}")
def admin_upload_status(upload_id: str, admin: User = Depends(get_admin_user)):
    row = UPLOAD_STATUS.get(_clean_upload_id(upload_id) or "")
    if not row:
        raise HTTPException(404, "上传状态不存在")
    return row


@router.post("/upload")
async def admin_upload_file(
    file: UploadFile = File(...),
    kind: str = Form("video"),
    upload_id: Optional[str] = Form(None),
    admin: User = Depends(get_admin_user),
):
    """上传课程封面图或课时视频到腾讯云 COS。"""
    upload_id = _clean_upload_id(upload_id)
    _set_upload_status(upload_id, "pending", kind=kind)
    try:
        result = await upload_fastapi_file(file, kind=kind, user_id=int(admin.id))
        if upload_id:
            result["upload_id"] = upload_id
        _set_upload_status(upload_id, "ok", kind=kind, result=result)
        return result
    except CosUploadError as exc:
        msg = str(exc)
        _set_upload_status(upload_id, "error", kind=kind, detail=msg)
        status_code = 500 if ("COS" in msg or "依赖" in msg or "配置" in msg or "安装" in msg) else 400
        raise HTTPException(status_code, msg)
    except Exception as exc:
        _set_upload_status(upload_id, "error", kind=kind, detail=str(exc))
        raise
