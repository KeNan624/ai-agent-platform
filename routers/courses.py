"""
Courses Router · 视频课程（用户端）
====================================
GET  /practice/courses              课程列表
GET  /practice/courses/{slug}       课程详情 + 课时列表
POST /practice/courses/{slug}/view  累加浏览量
GET  /practice/lessons/{id}         单个课时详情
POST /practice/lessons/{id}/view    累加课时浏览量
"""

from __future__ import annotations
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from auth import decode_access_token
from database import get_db
from models import PracticeCourse, PracticeLesson, User
from permissions import get_active_plan
from plan_config import get_practice_content_access

router = APIRouter(prefix="/practice", tags=["courses"])
optional_bearer_scheme = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════
#  Schemas
# ═══════════════════════════════════════════════

class LessonCard(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    lesson_type: str
    video_duration: int
    sort_order: int
    is_free: bool
    is_published: bool
    view_count: int


class CourseCard(BaseModel):
    slug: str
    title: str
    description: Optional[str] = None
    cover_url: Optional[str] = None
    cover_emoji: Optional[str] = None
    category: Optional[str] = None
    instructor: Optional[str] = None
    price: float
    status: str
    lesson_count: int
    total_duration: int
    view_count: int
    is_published: bool


class CourseDetail(CourseCard):
    lessons: list[LessonCard] = []


class LessonDetail(LessonCard):
    video_url: Optional[str] = None
    article_content: Optional[str] = None
    course_slug: str
    course_title: str


# ═══════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════

def _optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if credentials is None:
        return None
    token = credentials.credentials
    admin_token = os.getenv("ADMIN_TOKEN", "")
    if admin_token and token == admin_token:
        return User(id=0, phone="admin-token", is_admin=True, is_active=True)
    user_id = decode_access_token(token)
    if user_id is None:
        return None
    return db.query(User).filter(User.id == int(user_id), User.is_active == True).first()  # noqa: E712


def _allowed_course_ids(user: Optional[User], db: Session) -> Optional[list[int]]:
    if user is not None and getattr(user, "is_admin", False):
        return None
    plan_type = get_active_plan(user, db) if user is not None else "free"
    access = get_practice_content_access(plan_type, db)
    if not access["practice_access"]:
        return []
    if access["mode"] != "custom":
        return None
    return list(access["course_ids"])


def _require_course_access(user: Optional[User], course_id: int, db: Session) -> None:
    allowed_ids = _allowed_course_ids(user, db)
    if allowed_ids is None:
        return
    if int(course_id) in set(allowed_ids):
        return
    raise HTTPException(403, "当前套餐无权访问该内容")


def _course_to_card(c: PracticeCourse) -> CourseCard:
    return CourseCard(
        slug=c.slug, title=c.title, description=c.description,
        cover_url=c.cover_url, cover_emoji=c.cover_emoji,
        category=c.category, instructor=c.instructor,
        price=float(c.price or 0), status=c.status,
        lesson_count=c.lesson_count, total_duration=c.total_duration,
        view_count=c.view_count, is_published=c.is_published,
    )


def _lesson_to_card(l: PracticeLesson) -> LessonCard:
    return LessonCard(
        id=l.id, title=l.title, description=l.description,
        lesson_type=l.lesson_type, video_duration=l.video_duration,
        sort_order=l.sort_order, is_free=l.is_free,
        is_published=l.is_published, view_count=l.view_count,
    )


# ═══════════════════════════════════════════════
#  API
# ═══════════════════════════════════════════════

@router.get("/courses", response_model=list[CourseCard])
def list_courses(
    db: Session = Depends(get_db),
    category: Optional[str] = Query(None),
    current_user: Optional[User] = Depends(_optional_current_user),
):
    q = db.query(PracticeCourse).filter(
        PracticeCourse.is_published == True,  # noqa: E712
        PracticeCourse.content_kind == "course",
    )
    allowed_ids = _allowed_course_ids(current_user, db)
    if allowed_ids == []:
        return []
    if allowed_ids is not None:
        q = q.filter(PracticeCourse.id.in_(allowed_ids))
    if category:
        q = q.filter(PracticeCourse.category == category)
    q = q.order_by(PracticeCourse.sort_order.asc(), PracticeCourse.id.asc())
    return [_course_to_card(c) for c in q.all()]


@router.get("/courses/{slug}", response_model=CourseDetail)
def get_course(
    slug: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(_optional_current_user),
):
    c = db.query(PracticeCourse).options(
        joinedload(PracticeCourse.lessons)
    ).filter(PracticeCourse.slug == slug).first()
    if not c:
        raise HTTPException(404, "课程不存在")
    if not c.is_published:
        raise HTTPException(404, "课程未发布")
    _require_course_access(current_user, int(c.id), db)
    lessons = [_lesson_to_card(l) for l in (c.lessons or []) if l.is_published]
    return CourseDetail(**_course_to_card(c).model_dump(), lessons=lessons)


@router.post("/courses/{slug}/view")
def increment_course_view(slug: str, db: Session = Depends(get_db)):
    c = db.query(PracticeCourse).filter(PracticeCourse.slug == slug).first()
    if not c:
        raise HTTPException(404, "课程不存在")
    c.view_count = (c.view_count or 0) + 1
    db.commit()
    return {"ok": True, "view_count": c.view_count}


@router.get("/lessons/{lesson_id}", response_model=LessonDetail)
def get_lesson(
    lesson_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(_optional_current_user),
):
    l = db.query(PracticeLesson).options(
        joinedload(PracticeLesson.course)
    ).filter(PracticeLesson.id == lesson_id).first()
    if not l:
        raise HTTPException(404, "课时不存在")
    if not l.is_published:
        raise HTTPException(404, "课时未发布")
    _require_course_access(current_user, int(l.course_id), db)
    return LessonDetail(
        id=l.id, title=l.title, description=l.description,
        lesson_type=l.lesson_type, video_duration=l.video_duration,
        sort_order=l.sort_order, is_free=l.is_free,
        is_published=l.is_published, view_count=l.view_count,
        video_url=l.video_url, article_content=l.article_content,
        course_slug=l.course.slug, course_title=l.course.title,
    )


@router.post("/lessons/{lesson_id}/view")
def increment_lesson_view(lesson_id: int, db: Session = Depends(get_db)):
    l = db.query(PracticeLesson).filter(PracticeLesson.id == lesson_id).first()
    if not l:
        raise HTTPException(404, "课时不存在")
    l.view_count = (l.view_count or 0) + 1
    db.commit()
    return {"ok": True, "view_count": l.view_count}
