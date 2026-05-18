"""
routers/practice.py · 实战区 API
用项目已有的 SQLAlchemy Session（from database import get_db）
"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from urllib.parse import quote, unquote, urlparse
from app_config import get_app_setting
from auth import create_access_token, decode_access_token, ensure_default_admin, get_current_user, is_default_admin_phone
from database import get_db
from models import PracticeCourse, PracticeLesson, User
from permissions import get_active_plan
from plan_config import get_plan_definition, get_practice_content_access, plan_allows_practice, plan_can_publish_practice
from services.cos_upload import CosUploadError, upload_bytes, upload_fastapi_file
from services.feishu_import import FeishuImportError, import_feishu_docx, parse_feishu_docx_id
from sms_service import SmsVerificationError, verify_sms_code
import json
import logging
import os

router = APIRouter(prefix="/practice", tags=["practice"])
logger = logging.getLogger(__name__)
optional_bearer_scheme = HTTPBearer(auto_error=False)


class SubscribeRequest(BaseModel):
    phone: str
    project_slug: str
    source_page: Optional[str] = "practice"


# ─── 工具函数 ───
def row_to_dict(row):
    """将 SQLAlchemy Row 转为 dict"""
    if hasattr(row, '_mapping'):
        d = dict(row._mapping)
    elif hasattr(row, '_asdict'):
        d = row._asdict()
    else:
        d = dict(row)
    # datetime 转字符串
    for k in ('created_at', 'updated_at', 'notified_at'):
        if k in d and d[k] is not None:
            d[k] = str(d[k])
    # Decimal 转 float
    if 'price' in d and d['price'] is not None:
        d['price'] = float(d['price'])
    return d


def _plan_can_publish(plan: Optional[str]) -> bool:
    return plan_can_publish_practice(plan)


def _require_practice_publisher(user: User, db: Session) -> None:
    if getattr(user, "is_admin", False):
        return
    plan_type = get_active_plan(user, db)
    if plan_can_publish_practice(plan_type, db):
        return
    raise HTTPException(403, "当前套餐无实战区发布权限")


def _require_admin_user(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(403, "需要管理员权限")


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


def _allowed_content_ids(user: Optional[User], content_kind: str, db: Session) -> Optional[list[int]]:
    if user is not None and getattr(user, "is_admin", False):
        return None
    plan_type = get_active_plan(user, db) if user is not None else "free"
    access = get_practice_content_access(plan_type, db)
    if not access["practice_access"]:
        return []
    if access["mode"] != "custom":
        return None
    key = "column_ids" if content_kind == "column" else "course_ids"
    return list(access[key])


def _ids_sql(ids: list[int]) -> str:
    return ",".join(str(int(item)) for item in ids if int(item) > 0)


def _require_content_access(user: Optional[User], content_kind: str, content_id: int, db: Session) -> None:
    allowed_ids = _allowed_content_ids(user, content_kind, db)
    if allowed_ids is None:
        return
    if int(content_id) in set(allowed_ids):
        return
    raise HTTPException(403, "当前套餐无权访问该内容")


def _upload_error(exc: CosUploadError) -> HTTPException:
    msg = str(exc)
    status_code = 500 if ("COS" in msg or "依赖" in msg or "配置" in msg or "安装" in msg) else 400
    return HTTPException(status_code, msg)


def _practice_document_max_bytes() -> int:
    raw = (get_app_setting("PRACTICE_DOCUMENT_MAX_MB", "200") or "200").strip()
    try:
        return max(1, int(raw)) * 1024 * 1024
    except ValueError:
        return 200 * 1024 * 1024


def _allowed_pdf_preview_url(value: str) -> str:
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(400, "PDF 地址无效")

    parsed = parsed._replace(fragment="")
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path or "")
    if not path.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持预览 PDF 文件")

    allowed = False
    public_base = (get_app_setting("COS_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if public_base:
        base = urlparse(public_base)
        if (base.hostname or "").lower() == host:
            base_path = unquote(base.path or "").rstrip("/")
            allowed = not base_path or path.startswith(base_path + "/") or path == base_path

    bucket = (get_app_setting("COS_BUCKET", "") or "").strip()
    region = (get_app_setting("COS_REGION", "") or "").strip()
    if bucket and region and host == f"{bucket}.cos.{region}.myqcloud.com".lower():
        allowed = True

    if host.endswith(".myqcloud.com") and "/practice/" in path:
        allowed = True

    if not allowed:
        raise HTTPException(400, "PDF 预览仅支持本站上传的文件")
    return parsed.geturl()


def _pdf_inline_headers(source_url: str) -> dict[str, str]:
    filename = os.path.basename(unquote(urlparse(source_url).path)) or "preview.pdf"
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace("\\", "").replace('"', "") or "preview.pdf"
    return {
        "Content-Disposition": f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=300",
    }


def _first_markdown_image(content: str) -> str:
    import re
    img_match = re.search(r'!\[.*?\]\((.*?)\)', content or "")
    return img_match.group(1).strip() if img_match else ""


def _content_excerpt(content: str, limit: int = 500) -> str:
    import re
    text_content = re.sub(r'<video\b[^>]*>.*?</video>', ' ', content or "", flags=re.I | re.S)
    text_content = re.sub(r'!\[.*?\]\(.*?\)', ' ', text_content)
    text_content = re.sub(r'<[^>]+>', ' ', text_content)
    text_content = re.sub(r'[#*>\[\]`_-]+', ' ', text_content)
    text_content = re.sub(r'\s+', ' ', text_content).strip()
    return text_content[:limit]


def _display_name(user: User, fallback: Optional[str] = None) -> str:
    if getattr(user, "nickname", None):
        return user.nickname
    if fallback:
        return fallback
    phone = getattr(user, "phone", "") or ""
    return f"用户{phone[-4:]}" if phone else "用户"


def _practice_md_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "practice_md"))


def _feishu_sources_path() -> str:
    return os.path.join(_practice_md_dir(), "feishu_sources.json")


def _load_feishu_sources() -> dict[str, str]:
    try:
        with open(_feishu_sources_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def _save_feishu_sources(data: dict[str, str]) -> None:
    os.makedirs(_practice_md_dir(), exist_ok=True)
    tmp_path = _feishu_sources_path() + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _feishu_sources_path())


def _set_feishu_source(slug: str, url: str) -> None:
    sources = _load_feishu_sources()
    sources[slug] = url.strip()
    _save_feishu_sources(sources)


def _remove_feishu_source(slug: str) -> None:
    sources = _load_feishu_sources()
    if slug in sources:
        sources.pop(slug, None)
        _save_feishu_sources(sources)


# ─── 1. 项目列表 ───
@router.get("/pdf-preview")
async def preview_pdf(url: str = Query(...)):
    """Serve uploaded PDFs as same-origin inline previews for browser iframe rendering."""
    import httpx

    source_url = _allowed_pdf_preview_url(url)
    max_bytes = _practice_document_max_bytes()
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(
                source_url,
                headers={
                    "Accept": "application/pdf,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("PDF preview fetch failed: %s", exc)
        raise HTTPException(502, "PDF 预览加载失败")

    if response.status_code >= 400:
        raise HTTPException(502, "PDF 预览加载失败")
    if len(response.content) > max_bytes:
        raise HTTPException(413, f"PDF 文件过大，最大允许 {max_bytes // 1024 // 1024}MB")

    content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
    if content_type not in {"application/pdf", "application/x-pdf"} and not response.content.lstrip().startswith(b"%PDF"):
        raise HTTPException(400, "文件不是有效 PDF")

    return Response(
        content=response.content,
        media_type="application/pdf",
        headers=_pdf_inline_headers(source_url),
    )


@router.get("/projects")
def list_projects(
    project_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tag_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    is_featured: Optional[bool] = Query(None),
    keyword: Optional[str] = Query(None),
    sort: str = Query("hot"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    conditions = ["p.is_published = true"]
    params = {}

    if project_type:
        conditions.append("p.project_type = :project_type")
        params["project_type"] = project_type
    if category:
        conditions.append("p.category = :category")
        params["category"] = category
    if status:
        conditions.append("p.status = :status")
        params["status"] = status
    if is_featured is True:
        conditions.append("p.is_featured = true")
    if keyword:
        conditions.append("(p.title ILIKE :kw OR p.description ILIKE :kw OR p.author_name ILIKE :kw)")
        params["kw"] = f"%{keyword}%"

    join_clause = ""
    if tag_id:
        join_clause = "JOIN practice_project_tags pt ON pt.project_id = p.id AND pt.tag_id = :tag_id"
        params["tag_id"] = tag_id

    where = " AND ".join(conditions)
    order = "p.view_count DESC, p.sort_order ASC" if sort == "hot" else "p.created_at DESC"
    offset = (page - 1) * page_size

    count_sql = f"SELECT COUNT(*) as cnt FROM practice_projects p {join_clause} WHERE {where}"
    total = db.execute(text(count_sql), params).scalar() or 0

    list_sql = f"""
        SELECT p.*,
               COALESCE((SELECT COUNT(*) FROM practice_subscribes s WHERE s.project_slug = p.slug), 0) as subscribe_count
        FROM practice_projects p
        {join_clause}
        WHERE {where}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
    """
    params["limit"] = page_size
    params["offset"] = offset

    rows = db.execute(text(list_sql), params).fetchall()
    items = [row_to_dict(r) for r in rows]

    # 给每个 item 补充 first_image（从 md 文件抓）
    import re
    md_dir = os.path.join(os.path.dirname(__file__), "..", "practice_md")
    for item in items:
        item["first_image"] = ""
        if item.get("md_filename"):
            md_path = os.path.join(md_dir, item["md_filename"])
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    content = f.read(2000)  # 只读前 2000 字符
                    img_match = re.search(r'!\[.*?\]\((.*?)\)', content)
                    if img_match:
                        item["first_image"] = img_match.group(1)
            except:
                pass

    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ─── 2. 项目详情 ───
@router.get("/projects/{slug}")
def get_project(slug: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT * FROM practice_projects WHERE slug = :slug AND is_published = true"),
        {"slug": slug}
    ).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")

    item = row_to_dict(row)

    # 标签
    tags = db.execute(
        text("""SELECT t.id, t.name, t.tag_type FROM practice_tags t
                JOIN practice_project_tags pt ON pt.tag_id = t.id
                WHERE pt.project_id = :pid"""),
        {"pid": item["id"]}
    ).fetchall()
    item["tags"] = [row_to_dict(t) for t in tags]

    # 订阅数
    sub_count = db.execute(
        text("SELECT COUNT(*) FROM practice_subscribes WHERE project_slug = :slug"),
        {"slug": slug}
    ).scalar() or 0
    item["subscribe_count"] = sub_count

    return item


# ─── 3. 项目 Markdown 内容 ───
@router.get("/projects/{slug}/md")
def get_project_md(slug: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT md_filename FROM practice_projects WHERE slug = :slug AND is_published = true"),
        {"slug": slug}
    ).fetchone()
    if not row or not row[0]:
        return {"content": "内容即将更新..."}

    md_path = os.path.join(os.path.dirname(__file__), "..", "practice_md", row[0])
    if not os.path.exists(md_path):
        return {"content": "内容即将更新..."}

    with open(md_path, "r", encoding="utf-8") as f:
        return {"content": f.read()}


# ─── 4. 标签列表 ───
@router.get("/tags")
def list_tags(tag_type: Optional[str] = Query(None), db: Session = Depends(get_db)):
    if tag_type:
        rows = db.execute(
            text("SELECT * FROM practice_tags WHERE tag_type = :tt ORDER BY sort_order, id"),
            {"tt": tag_type}
        ).fetchall()
    else:
        rows = db.execute(text("SELECT * FROM practice_tags ORDER BY sort_order, id")).fetchall()
    return [row_to_dict(r) for r in rows]


# ─── 5. 课程列表 ───
@router.get("/courses")
def list_courses(
    category: Optional[str] = Query(None),
    is_free: Optional[bool] = Query(None),
    sort: str = Query("hot"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    conditions = ["is_published = true", "COALESCE(content_kind, 'course') = 'course'"]
    params = {}
    allowed_ids = _allowed_content_ids(current_user, "course", db)
    if allowed_ids == []:
        return {"total": 0, "page": page, "page_size": page_size, "items": []}
    if allowed_ids is not None:
        conditions.append(f"id IN ({_ids_sql(allowed_ids)})")

    if category:
        conditions.append("category = :category")
        params["category"] = category
    if is_free is True:
        conditions.append("price = 0")
    elif is_free is False:
        conditions.append("price > 0")

    where = " AND ".join(conditions)
    order = "view_count DESC, sort_order ASC" if sort == "hot" else "created_at DESC"

    total = db.execute(text(f"SELECT COUNT(*) FROM practice_courses pc WHERE {where}"), params).scalar() or 0

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    rows = db.execute(text(f"""
        SELECT * FROM practice_courses WHERE {where}
        ORDER BY {order} LIMIT :limit OFFSET :offset
    """), params).fetchall()

    return {"total": total, "page": page, "page_size": page_size, "items": [row_to_dict(r) for r in rows]}


# ─── 6. 课程详情 + 课时 ───
@router.get("/courses/{slug}")
def get_course(
    slug: str,
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("""
            SELECT * FROM practice_courses
            WHERE slug = :slug AND is_published = true
              AND COALESCE(content_kind, 'course') = 'course'
        """),
        {"slug": slug}
    ).fetchone()
    if not row:
        raise HTTPException(404, "课程不存在")

    item = row_to_dict(row)
    _require_content_access(current_user, "course", int(item["id"]), db)

    lessons = db.execute(text("""
        SELECT id, title, description, lesson_type, video_duration, sort_order, is_free, is_published, view_count
        FROM practice_lessons
        WHERE course_id = :cid AND is_published = true
        ORDER BY sort_order, id
    """), {"cid": item["id"]}).fetchall()
    item["lessons"] = [row_to_dict(l) for l in lessons]

    return item


# ─── 7. 单课时详情 ───
@router.get("/courses/{slug}/lessons/{lesson_id}")
def get_lesson(
    slug: str,
    lesson_id: int,
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    course = db.execute(
        text("""
            SELECT id FROM practice_courses
            WHERE slug = :slug AND is_published = true
              AND COALESCE(content_kind, 'course') = 'course'
        """),
        {"slug": slug}
    ).fetchone()
    if not course:
        raise HTTPException(404, "课程不存在")
    _require_content_access(current_user, "course", int(course[0]), db)

    lesson = db.execute(text("""
        SELECT * FROM practice_lessons
        WHERE id = :lid AND course_id = :cid AND is_published = true
    """), {"lid": lesson_id, "cid": course[0]}).fetchone()
    if not lesson:
        raise HTTPException(404, "课时不存在")

    # 增加浏览量
    db.execute(text("UPDATE practice_lessons SET view_count = view_count + 1 WHERE id = :lid"), {"lid": lesson_id})
    db.commit()

    return row_to_dict(lesson)


# ─── 7b. 专栏列表 / 详情 / 章节 ───
@router.get("/columns")
def list_columns(
    category: Optional[str] = Query(None),
    sort: str = Query("latest"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    conditions = ["is_published = true", "content_kind = 'column'"]
    params = {}
    allowed_ids = _allowed_content_ids(current_user, "column", db)
    if allowed_ids == []:
        return {"total": 0, "page": page, "page_size": page_size, "items": []}
    if allowed_ids is not None:
        conditions.append(f"pc.id IN ({_ids_sql(allowed_ids)})")
    if category:
        conditions.append("category = :category")
        params["category"] = category

    where = " AND ".join(conditions)
    order = "view_count DESC, sort_order ASC" if sort == "hot" else "updated_at DESC, sort_order ASC"
    total = db.execute(text(f"SELECT COUNT(*) FROM practice_courses pc WHERE {where}"), params).scalar() or 0
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    rows = db.execute(text(f"""
        SELECT pc.*,
          COALESCE((SELECT COUNT(*) FROM practice_lessons pl
                    WHERE pl.course_id = pc.id AND pl.is_published = true), 0) AS published_lesson_count,
          (SELECT MAX(updated_at) FROM practice_lessons pl WHERE pl.course_id = pc.id) AS last_lesson_updated_at
        FROM practice_courses pc
        WHERE {where}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
    """), params).fetchall()
    return {"total": total, "page": page, "page_size": page_size, "items": [row_to_dict(r) for r in rows]}


@router.get("/columns/{slug}")
def get_column(
    slug: str,
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(text("""
        SELECT * FROM practice_courses
        WHERE slug = :slug AND is_published = true AND content_kind = 'column'
    """), {"slug": slug}).fetchone()
    if not row:
        raise HTTPException(404, "专栏不存在")
    item = row_to_dict(row)
    _require_content_access(current_user, "column", int(item["id"]), db)
    lessons = db.execute(text("""
        SELECT id, title, description, lesson_type, video_duration, sort_order,
               is_free, is_published, view_count, source_type, source_doc_id
        FROM practice_lessons
        WHERE course_id = :cid AND is_published = true
        ORDER BY sort_order, id
    """), {"cid": item["id"]}).fetchall()
    item["lessons"] = [row_to_dict(l) for l in lessons]
    return item


@router.get("/columns/{slug}/lessons/{lesson_id}")
def get_column_lesson(
    slug: str,
    lesson_id: int,
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    course = db.execute(text("""
        SELECT id FROM practice_courses
        WHERE slug = :slug AND is_published = true AND content_kind = 'column'
    """), {"slug": slug}).fetchone()
    if not course:
        raise HTTPException(404, "专栏不存在")
    _require_content_access(current_user, "column", int(course[0]), db)
    lesson = db.execute(text("""
        SELECT * FROM practice_lessons
        WHERE id = :lid AND course_id = :cid AND is_published = true
    """), {"lid": lesson_id, "cid": course[0]}).fetchone()
    if not lesson:
        raise HTTPException(404, "章节不存在")
    db.execute(text("UPDATE practice_lessons SET view_count = view_count + 1 WHERE id = :lid"), {"lid": lesson_id})
    db.commit()
    return row_to_dict(lesson)


# ─── 8. 报名订阅 ───
@router.post("/subscribe")
def subscribe(req: SubscribeRequest, db: Session = Depends(get_db)):
    existing = db.execute(
        text("SELECT id FROM practice_subscribes WHERE phone = :phone AND project_slug = :slug"),
        {"phone": req.phone, "slug": req.project_slug}
    ).fetchone()
    if existing:
        return {"ok": True, "message": "已订阅", "id": existing[0]}

    result = db.execute(text("""
        INSERT INTO practice_subscribes (phone, project_slug, source_page, created_at)
        VALUES (:phone, :slug, :source, NOW()) RETURNING id
    """), {"phone": req.phone, "slug": req.project_slug, "source": req.source_page})
    db.commit()
    new_id = result.fetchone()[0]
    return {"ok": True, "message": "订阅成功", "id": new_id}


# ─── 9. 检查订阅状态 ───
@router.get("/subscribe/check")
def check_subscribe(phone: str = Query(...), project_slug: str = Query(...), db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT id FROM practice_subscribes WHERE phone = :phone AND project_slug = :slug"),
        {"phone": phone, "slug": project_slug}
    ).fetchone()
    return {"subscribed": row is not None}


# ─── 10. 分类列表 ───
@router.get("/categories")
def list_categories(project_type: Optional[str] = Query(None), db: Session = Depends(get_db)):
    if project_type:
        rows = db.execute(text("""
            SELECT DISTINCT category FROM practice_projects
            WHERE is_published = true AND project_type = :pt AND category IS NOT NULL
            ORDER BY category
        """), {"pt": project_type}).fetchall()
    else:
        rows = db.execute(text("""
            SELECT DISTINCT category FROM practice_projects
            WHERE is_published = true AND category IS NOT NULL ORDER BY category
        """)).fetchall()
    return [r[0] for r in rows]


# ─── 11. 课程分类 ───
@router.get("/course-categories")
def list_course_categories(
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    allowed_ids = _allowed_content_ids(current_user, "course", db)
    if allowed_ids == []:
        return []
    extra = f"AND id IN ({_ids_sql(allowed_ids)})" if allowed_ids is not None else ""
    rows = db.execute(text("""
        SELECT DISTINCT category FROM practice_courses
        WHERE is_published = true
          AND COALESCE(content_kind, 'course') = 'course'
          AND category IS NOT NULL
          """ + extra + """
        ORDER BY category
    """)).fetchall()
    return [r[0] for r in rows]


@router.get("/column-categories")
def list_column_categories(
    current_user: Optional[User] = Depends(_optional_current_user),
    db: Session = Depends(get_db),
):
    allowed_ids = _allowed_content_ids(current_user, "column", db)
    if allowed_ids == []:
        return []
    extra = f"AND id IN ({_ids_sql(allowed_ids)})" if allowed_ids is not None else ""
    rows = db.execute(text("""
        SELECT DISTINCT category FROM practice_courses
        WHERE is_published = true
          AND content_kind = 'column'
          AND category IS NOT NULL
          """ + extra + """
        ORDER BY category
    """)).fetchall()
    return [r[0] for r in rows]


# ─── 12. 点赞 ───
class LikeRequest(BaseModel):
    user_id: int
    project_slug: str

@router.post("/like")
def toggle_like(req: LikeRequest, db: Session = Depends(get_db)):
    existing = db.execute(
        text("SELECT id FROM practice_likes WHERE user_id = :uid AND project_slug = :slug"),
        {"uid": req.user_id, "slug": req.project_slug}
    ).fetchone()
    if existing:
        db.execute(text("DELETE FROM practice_likes WHERE id = :id"), {"id": existing[0]})
        db.commit()
        count = db.execute(text("SELECT COUNT(*) FROM practice_likes WHERE project_slug = :slug"), {"slug": req.project_slug}).scalar()
        return {"liked": False, "count": count}
    else:
        db.execute(text("""
            INSERT INTO practice_likes (user_id, project_slug, created_at) VALUES (:uid, :slug, NOW())
        """), {"uid": req.user_id, "slug": req.project_slug})
        db.commit()
        count = db.execute(text("SELECT COUNT(*) FROM practice_likes WHERE project_slug = :slug"), {"slug": req.project_slug}).scalar()
        return {"liked": True, "count": count}

@router.get("/like/status")
def like_status(user_id: int = Query(...), project_slug: str = Query(...), db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT id FROM practice_likes WHERE user_id = :uid AND project_slug = :slug"),
        {"uid": user_id, "slug": project_slug}
    ).fetchone()
    count = db.execute(text("SELECT COUNT(*) FROM practice_likes WHERE project_slug = :slug"), {"slug": project_slug}).scalar()
    return {"liked": row is not None, "count": count or 0}


# ─── 13. 评论 ───
class CommentRequest(BaseModel):
    user_id: int
    username: str
    project_slug: str
    content: str

@router.post("/comments")
def add_comment(req: CommentRequest, db: Session = Depends(get_db)):
    if not req.content.strip():
        raise HTTPException(400, "评论内容不能为空")
    result = db.execute(text("""
        INSERT INTO practice_comments (user_id, username, project_slug, content, created_at)
        VALUES (:uid, :uname, :slug, :content, NOW()) RETURNING id, created_at
    """), {"uid": req.user_id, "uname": req.username, "slug": req.project_slug, "content": req.content.strip()})
    db.commit()
    row = result.fetchone()
    return {"id": row[0], "created_at": str(row[1]), "username": req.username, "content": req.content.strip()}

@router.get("/comments")
def list_comments(project_slug: str = Query(...), page: int = Query(1, ge=1), page_size: int = Query(50), db: Session = Depends(get_db)):
    total = db.execute(text("SELECT COUNT(*) FROM practice_comments WHERE project_slug = :slug"), {"slug": project_slug}).scalar() or 0
    rows = db.execute(text("""
        SELECT id, user_id, username, content, created_at FROM practice_comments
        WHERE project_slug = :slug ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), {"slug": project_slug, "limit": page_size, "offset": (page - 1) * page_size}).fetchall()
    items = []
    for r in rows:
        d = row_to_dict(r)
        items.append(d)
    return {"total": total, "items": items}


# ─── 14. 浏览量 +1（每用户只计一次） ───
class ViewRequest(BaseModel):
    user_id: Optional[int] = None

@router.post("/projects/{slug}/view")
def increment_view(slug: str, req: ViewRequest = None, db: Session = Depends(get_db)):
    uid = req.user_id if req else None
    if uid:
        existing = db.execute(
            text("SELECT id FROM practice_views WHERE user_id = :uid AND project_slug = :slug"),
            {"uid": uid, "slug": slug}
        ).fetchone()
        if existing:
            return {"ok": True, "new": False}
        db.execute(text("""
            INSERT INTO practice_views (user_id, project_slug, created_at) VALUES (:uid, :slug, NOW())
        """), {"uid": uid, "slug": slug})
    db.execute(text("UPDATE practice_projects SET view_count = view_count + 1 WHERE slug = :slug"), {"slug": slug})
    db.commit()
    return {"ok": True, "new": True}


# ─── 15. 权限检查（按后台套餐配置） ───
@router.get("/access-check")
def check_access(user_id: int = Query(...), db: Session = Depends(get_db)):
    # 检查管理员
    user = db.execute(
        text("SELECT id, nickname, is_admin FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user:
        return {"allowed": False, "reason": "用户不存在", "plan": None}

    if user[2]:  # is_admin
        return {"allowed": True, "plan": "admin", "can_publish": True}

    # 检查会员
    membership = db.execute(text("""
        SELECT plan_type, expire_at, status FROM memberships
        WHERE user_id = :uid AND status = 'active'
          AND (expire_at IS NULL OR expire_at > NOW())
        ORDER BY expire_at DESC NULLS LAST LIMIT 1
    """), {"uid": user_id}).fetchone()

    plan = membership[0] if membership else "free"
    plan_cfg = get_plan_definition(plan, db)
    allowed = plan_allows_practice(plan, db)
    can_publish = plan_can_publish_practice(plan, db)
    if allowed:
        return {
            "allowed": True,
            "plan": plan,
            "plan_name": plan_cfg["name"],
            "can_publish": can_publish,
        }
    return {
        "allowed": False,
        "reason": "当前套餐无实战区访问权限",
        "plan": plan,
        "plan_name": plan_cfg["name"],
        "can_publish": False,
    }


# ─── 16. 用户发布内容 ───
class PublishRequest(BaseModel):
    user_id: Optional[int] = None  # 兼容旧前端；后端不信任该字段
    username: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    project_type: str = "experience"


class CoursePublishRequest(BaseModel):
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    video_url: str
    video_duration: int = 0
    cover_url: Optional[str] = None
    cover_emoji: Optional[str] = "🎬"
    instructor: Optional[str] = None

@router.post("/publish")
def publish_content(
    req: PublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_practice_publisher(current_user, db)
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(400, "标题和内容不能为空")

    # 生成 slug
    import time, hashlib
    slug = "user-" + hashlib.md5(f"{current_user.id}-{time.time()}".encode()).hexdigest()[:12]

    # 写 md 文件
    md_filename = slug + ".md"
    md_dir = os.path.join(os.path.dirname(__file__), "..", "practice_md")
    os.makedirs(md_dir, exist_ok=True)
    md_path = os.path.join(md_dir, md_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(req.content)

    # 插入数据库 — description 存摘要；封面图由后台管理员单独维护
    description = _content_excerpt(req.content)
    author_name = _display_name(current_user, req.username)

    project_type = req.project_type if req.project_type in {"experience", "camp"} else "experience"
    status = "ongoing" if project_type == "camp" else "published"
    cover_emoji = "📚" if project_type == "camp" else "📝"

    result = db.execute(text("""
        INSERT INTO practice_projects
            (slug, title, description, project_type, category, status,
             cover_emoji, cover_color, cover_image, md_filename, sort_order, is_published,
             is_featured, view_count, author_id, author_name, created_at, updated_at)
        VALUES
            (:slug, :title, :desc, :ptype, :cat, :status,
             :cover_emoji, '#F5EFDE', :cover_image, :md, 0, true,
             false, 0, :uid, :uname, NOW(), NOW())
        RETURNING id
    """), {
        "slug": slug,
        "title": req.title.strip(),
        "desc": description,
        "ptype": project_type,
        "cat": req.category,
        "status": status,
        "cover_emoji": cover_emoji,
        "cover_image": None,
        "md": md_filename,
        "uid": current_user.id,
        "uname": author_name
    })
    db.commit()
    new_id = result.fetchone()[0]

    return {"ok": True, "id": new_id, "slug": slug}


@router.post("/course-publish")
def publish_video_course(
    req: CoursePublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_practice_publisher(current_user, db)
    if not req.title.strip():
        raise HTTPException(400, "课程标题不能为空")
    if not req.video_url.strip():
        raise HTTPException(400, "请先上传视频")

    import hashlib
    import re
    import time

    base_slug = re.sub(r"[^a-z0-9]+", "-", req.title.strip().lower()).strip("-")[:40]
    if not base_slug:
        base_slug = "video-course"
    slug = f"{base_slug}-{hashlib.md5(f'{current_user.id}-{time.time()}'.encode()).hexdigest()[:8]}"
    instructor = req.instructor or _display_name(current_user)
    course = PracticeCourse(
        slug=slug,
        title=req.title.strip(),
        description=(req.description or "").strip()[:500] or None,
        cover_url=req.cover_url,
        cover_emoji=req.cover_emoji or "🎬",
        category=req.category,
        instructor=instructor,
        content_kind="course",
        price=0,
        status="published",
        sort_order=0,
        lesson_count=1,
        total_duration=max(0, int(req.video_duration or 0)),
        is_published=True,
    )
    db.add(course)
    db.flush()
    lesson = PracticeLesson(
        course_id=course.id,
        title=req.title.strip(),
        description=(req.description or "").strip()[:500] or None,
        lesson_type="video",
        video_url=req.video_url.strip(),
        video_duration=max(0, int(req.video_duration or 0)),
        sort_order=0,
        is_free=True,
        is_published=True,
    )
    db.add(lesson)
    db.commit()
    db.refresh(course)
    return {"ok": True, "id": course.id, "slug": course.slug, "lesson_id": lesson.id}


# ─── 17. 媒体上传 ───
@router.post("/upload-media")
async def upload_media(
    kind: str = Form("image"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_practice_publisher(current_user, db)
    try:
        return await upload_fastapi_file(file, kind=kind, user_id=int(current_user.id))
    except CosUploadError as exc:
        raise _upload_error(exc)


@router.post("/upload-image")
async def upload_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_practice_publisher(current_user, db)
    try:
        return await upload_fastapi_file(file, kind="image", user_id=int(current_user.id))
    except CosUploadError as exc:
        raise _upload_error(exc)


# ─── 17b. 代理下载外部图片到本地 ───
class ProxyImageRequest(BaseModel):
    image_url: str

@router.post("/proxy-image")
async def proxy_image(
    req: ProxyImageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_practice_publisher(current_user, db)
    import httpx
    from urllib.parse import urlparse

    parsed = urlparse(req.image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "图片地址无效")
    try:
        # 模拟浏览器请求头绕过防盗链
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(req.image_url, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "image/png")
        filename = os.path.basename(parsed.path) or "proxy-image"
        return await upload_bytes(
            response.content,
            filename=filename,
            kind="image",
            user_id=int(current_user.id),
            content_type=content_type,
        )
    except CosUploadError as exc:
        raise _upload_error(exc)
    except Exception as e:
        raise HTTPException(400, f"图片下载失败: {str(e)}")


class EditRequest(BaseModel):
    user_id: Optional[int] = None  # 兼容旧前端；后端不信任该字段
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None

@router.put("/projects/{slug}")
def edit_project(
    slug: str,
    req: EditRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("SELECT id, author_id, md_filename FROM practice_projects WHERE slug = :slug"),
        {"slug": slug}
    ).fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")

    # 权限检查：作者本人或管理员
    if row[1] != current_user.id and not getattr(current_user, "is_admin", False):
        raise HTTPException(403, "无权编辑")

    updates = []
    params = {"slug": slug}

    if req.title is not None:
        if not req.title.strip():
            raise HTTPException(400, "标题不能为空")
        updates.append("title = :title")
        params["title"] = req.title.strip()
    if req.category is not None:
        updates.append("category = :category")
        params["category"] = req.category or None
    if req.content is not None:
        if not req.content.strip():
            raise HTTPException(400, "正文不能为空")
        updates.append("description = :desc")
        params["desc"] = _content_excerpt(req.content)
        # 更新 md 文件
        md_filename = row[2] or f"{slug}.md"
        md_dir = os.path.join(os.path.dirname(__file__), "..", "practice_md")
        os.makedirs(md_dir, exist_ok=True)
        md_path = os.path.join(md_dir, md_filename)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        if not row[2]:
            updates.append("md_filename = :md_filename")
            params["md_filename"] = md_filename

    if updates:
        updates.append("updated_at = NOW()")
        sql = f"UPDATE practice_projects SET {', '.join(updates)} WHERE slug = :slug"
        db.execute(text(sql), params)
        db.commit()

    return {"ok": True}


# ─── 19. 删除内容（软删除 · 仅作者或管理员） ───
class DeleteRequest(BaseModel):
    user_id: Optional[int] = None  # 兼容旧前端；后端不信任该字段

@router.delete("/projects/{slug}")
def delete_project(
    slug: str,
    req: Optional[DeleteRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("SELECT id, author_id FROM practice_projects WHERE slug = :slug"),
        {"slug": slug}
    ).fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")

    if row[1] != current_user.id and not getattr(current_user, "is_admin", False):
        raise HTTPException(403, "无权删除")

    db.execute(text("UPDATE practice_projects SET is_published = false WHERE slug = :slug"), {"slug": slug})
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════
# 管理后台 API（需要 is_admin）
# ═══════════════════════════════════════

def _check_admin(user_id: int, db: Session):
    row = db.execute(text("SELECT is_admin FROM users WHERE id = :uid"), {"uid": user_id}).fetchone()
    if not row or not row[0]:
        raise HTTPException(403, "需要管理员权限")


class FeishuImportRequest(BaseModel):
    url: str
    category: Optional[str] = None
    is_published: bool = False


class ColumnCreateRequest(BaseModel):
    slug: Optional[str] = None
    title: str
    description: Optional[str] = None
    cover_url: Optional[str] = None
    category: Optional[str] = None
    instructor: Optional[str] = None
    sort_order: int = 0
    is_published: bool = False


class ColumnUpdateRequest(BaseModel):
    slug: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    category: Optional[str] = None
    instructor: Optional[str] = None
    sort_order: Optional[int] = None
    status: Optional[str] = None
    is_published: Optional[bool] = None


class ColumnLessonCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    lesson_type: str = "article"
    video_url: Optional[str] = None
    video_duration: int = 0
    article_content: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    source_doc_id: Optional[str] = None
    sort_order: Optional[int] = None
    is_free: bool = False
    is_published: bool = True


class ColumnLessonUpdateRequest(BaseModel):
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


class ColumnLessonReorderRequest(BaseModel):
    items: list[dict] = []


class ColumnFeishuImportRequest(BaseModel):
    url: str
    sort_order: Optional[int] = None
    is_free: bool = False
    is_published: bool = True


class ColumnFeishuSyncRequest(BaseModel):
    url: Optional[str] = None


def _slugify_title(title: str, fallback: str = "column") -> str:
    import hashlib
    import re
    import time

    base = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")[:44]
    if not base:
        base = fallback
    suffix = hashlib.md5(f"{title}-{time.time()}".encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def _next_lesson_sort(db: Session, course_id: int) -> int:
    row = db.execute(
        text("SELECT COALESCE(MAX(sort_order), -10) + 10 FROM practice_lessons WHERE course_id = :cid"),
        {"cid": course_id},
    ).fetchone()
    return int(row[0] or 0)


def _validate_column_lesson_media(lesson_type: str, video_url: Optional[str]) -> None:
    if lesson_type not in {"article", "video"}:
        raise HTTPException(400, "章节类型只能是 article 或 video")
    if lesson_type == "video" and not (video_url or "").strip():
        raise HTTPException(400, "视频章节必须先上传视频或填写视频链接")


def _refresh_course_stats(db: Session, course: PracticeCourse) -> None:
    rows = db.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(CASE WHEN lesson_type = 'video' THEN video_duration ELSE 0 END), 0)
        FROM practice_lessons
        WHERE course_id = :cid
    """), {"cid": course.id}).fetchone()
    course.lesson_count = int(rows[0] or 0)
    course.total_duration = int(rows[1] or 0)
    course.updated_at = datetime.utcnow()


def _column_to_dict(c: PracticeCourse) -> dict:
    return {
        "id": c.id,
        "slug": c.slug,
        "title": c.title,
        "description": c.description,
        "cover_url": c.cover_url,
        "cover_emoji": c.cover_emoji,
        "category": c.category,
        "instructor": c.instructor,
        "content_kind": c.content_kind or "column",
        "price": float(c.price or 0),
        "status": c.status,
        "sort_order": c.sort_order,
        "lesson_count": c.lesson_count,
        "total_duration": c.total_duration,
        "view_count": c.view_count,
        "is_published": c.is_published,
        "created_at": str(c.created_at) if c.created_at else None,
        "updated_at": str(c.updated_at) if c.updated_at else None,
    }


def _lesson_to_dict(l: PracticeLesson) -> dict:
    return {
        "id": l.id,
        "course_id": l.course_id,
        "title": l.title,
        "description": l.description,
        "lesson_type": l.lesson_type,
        "video_url": l.video_url,
        "video_duration": l.video_duration,
        "article_content": l.article_content,
        "source_type": l.source_type,
        "source_url": l.source_url,
        "source_doc_id": l.source_doc_id,
        "sort_order": l.sort_order,
        "is_free": l.is_free,
        "is_published": l.is_published,
        "view_count": l.view_count,
        "created_at": str(l.created_at) if l.created_at else None,
        "updated_at": str(l.updated_at) if l.updated_at else None,
    }


@router.post("/admin/import-feishu")
async def admin_import_feishu(
    req: FeishuImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    try:
        imported = await import_feishu_docx(req.url, user_id=int(current_user.id))
    except FeishuImportError as exc:
        logger.warning("Feishu project import failed: %s", exc)
        raise HTTPException(400, str(exc))

    import hashlib
    import time

    slug = "feishu-" + hashlib.md5(
        f"{current_user.id}-{req.url}-{time.time()}".encode("utf-8")
    ).hexdigest()[:12]
    md_filename = slug + ".md"
    md_dir = os.path.join(os.path.dirname(__file__), "..", "practice_md")
    os.makedirs(md_dir, exist_ok=True)
    md_path = os.path.join(md_dir, md_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(imported.markdown)

    description = _content_excerpt(imported.markdown)
    author_name = _display_name(current_user, "阿川")

    result = db.execute(text("""
        INSERT INTO practice_projects
            (slug, title, description, project_type, category, status,
             cover_emoji, cover_color, cover_image, md_filename, sort_order, is_published,
             is_featured, view_count, author_id, author_name, created_at, updated_at)
        VALUES
            (:slug, :title, :desc, 'experience', :cat, 'published',
             '📝', '#F5EFDE', :cover_image, :md, 0, :is_published,
             false, 0, :uid, :uname, NOW(), NOW())
        RETURNING id
    """), {
        "slug": slug,
        "title": imported.title,
        "desc": description,
        "cat": req.category or None,
        "cover_image": None,
        "md": md_filename,
        "is_published": bool(req.is_published),
        "uid": current_user.id,
        "uname": author_name,
    })
    db.commit()
    new_id = result.fetchone()[0]
    _set_feishu_source(slug, req.url)
    return {
        "ok": True,
        "id": new_id,
        "slug": slug,
        "title": imported.title,
        "image_count": imported.image_count,
        "pdf_count": imported.pdf_count,
        "style_count": imported.style_count,
        "callout_count": imported.callout_count,
        "warning_count": len(imported.warnings),
        "warnings": imported.warnings,
    }


# ─── C1. 管理员专栏 CRUD ───
@router.get("/admin/columns")
def admin_list_columns(
    keyword: Optional[str] = Query(None),
    is_published: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    query = db.query(PracticeCourse).filter(PracticeCourse.content_kind == "column")
    if is_published is not None:
        query = query.filter(PracticeCourse.is_published == is_published)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.filter(PracticeCourse.title.ilike(like))
    total = query.count()
    items = query.order_by(
        PracticeCourse.sort_order.asc(),
        PracticeCourse.updated_at.desc(),
        PracticeCourse.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_column_to_dict(c) for c in items]}


@router.post("/admin/columns")
def admin_create_column(
    req: ColumnCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "专栏标题不能为空")
    slug = (req.slug or "").strip() or _slugify_title(title, "column")
    if db.query(PracticeCourse).filter(PracticeCourse.slug == slug).first():
        raise HTTPException(400, f"slug '{slug}' 已存在")
    course = PracticeCourse(
        slug=slug,
        title=title,
        description=(req.description or "").strip()[:500] or None,
        cover_url=(req.cover_url or "").strip() or None,
        cover_emoji="📚",
        category=(req.category or "").strip() or None,
        instructor=(req.instructor or "").strip() or "阿川",
        content_kind="column",
        price=0,
        sort_order=req.sort_order,
        status="published" if req.is_published else "draft",
        is_published=bool(req.is_published),
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return {"ok": True, "item": _column_to_dict(course)}


@router.put("/admin/columns/{column_id}")
def admin_update_column(
    column_id: int,
    req: ColumnUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    course = db.query(PracticeCourse).filter(
        PracticeCourse.id == column_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not course:
        raise HTTPException(404, "专栏不存在")
    data = req.model_dump(exclude_none=True)
    if "slug" in data:
        next_slug = (data["slug"] or "").strip()
        if not next_slug:
            raise HTTPException(400, "slug 不能为空")
        exists = db.query(PracticeCourse).filter(
            PracticeCourse.slug == next_slug,
            PracticeCourse.id != column_id,
        ).first()
        if exists:
            raise HTTPException(400, f"slug '{next_slug}' 已存在")
        course.slug = next_slug
        data.pop("slug", None)
    for field, value in data.items():
        if field in {"title", "description", "cover_url", "category", "instructor", "status"} and isinstance(value, str):
            value = value.strip() or None
        setattr(course, field, value)
    if req.is_published is True:
        course.status = "published"
    elif req.is_published is False and req.status is None:
        course.status = "draft"
    course.content_kind = "column"
    course.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(course)
    return {"ok": True, "item": _column_to_dict(course)}


@router.delete("/admin/columns/{column_id}")
def admin_delete_column(
    column_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    course = db.query(PracticeCourse).filter(
        PracticeCourse.id == column_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not course:
        raise HTTPException(404, "专栏不存在")
    db.query(PracticeLesson).filter(PracticeLesson.course_id == column_id).delete(synchronize_session=False)
    db.delete(course)
    db.commit()
    return {"ok": True}


@router.get("/admin/columns/{column_id}/lessons")
def admin_list_column_lessons(
    column_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    course = db.query(PracticeCourse).filter(
        PracticeCourse.id == column_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not course:
        raise HTTPException(404, "专栏不存在")
    lessons = db.query(PracticeLesson).filter(
        PracticeLesson.course_id == column_id,
    ).order_by(PracticeLesson.sort_order.asc(), PracticeLesson.id.asc()).all()
    return {"column": _column_to_dict(course), "items": [_lesson_to_dict(l) for l in lessons]}


@router.post("/admin/columns/{column_id}/lessons")
def admin_create_column_lesson(
    column_id: int,
    req: ColumnLessonCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    course = db.query(PracticeCourse).filter(
        PracticeCourse.id == column_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not course:
        raise HTTPException(404, "专栏不存在")
    lesson_type = req.lesson_type or "article"
    video_url = (req.video_url or "").strip() or None
    _validate_column_lesson_media(lesson_type, video_url)
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "章节标题不能为空")
    lesson = PracticeLesson(
        course_id=column_id,
        title=title,
        description=(req.description or "").strip()[:500] or None,
        lesson_type=lesson_type,
        video_url=video_url,
        video_duration=max(0, int(req.video_duration or 0)),
        article_content=req.article_content,
        source_type=req.source_type,
        source_url=req.source_url,
        source_doc_id=req.source_doc_id,
        sort_order=req.sort_order if req.sort_order is not None else _next_lesson_sort(db, column_id),
        is_free=bool(req.is_free),
        is_published=bool(req.is_published),
    )
    db.add(lesson)
    db.flush()
    _refresh_course_stats(db, course)
    db.commit()
    db.refresh(lesson)
    return {"ok": True, "item": _lesson_to_dict(lesson)}


@router.put("/admin/columns/lessons/{lesson_id}")
def admin_update_column_lesson(
    lesson_id: int,
    req: ColumnLessonUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    lesson = db.query(PracticeLesson).join(PracticeCourse).filter(
        PracticeLesson.id == lesson_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not lesson:
        raise HTTPException(404, "章节不存在")
    data = req.model_dump(exclude_none=True)
    next_type = data.get("lesson_type", lesson.lesson_type)
    if "video_url" in data:
        data["video_url"] = (data["video_url"] or "").strip() or None
    _validate_column_lesson_media(next_type, data.get("video_url", lesson.video_url))
    if "title" in data and not data["title"].strip():
        raise HTTPException(400, "章节标题不能为空")
    for field, value in data.items():
        if isinstance(value, str) and field in {"title", "description", "video_url", "source_type", "source_url", "source_doc_id"}:
            value = value.strip() or None
        if field == "video_duration" and value is not None:
            value = max(0, int(value))
        setattr(lesson, field, value)
    lesson.updated_at = datetime.utcnow()
    course = db.query(PracticeCourse).filter(PracticeCourse.id == lesson.course_id).first()
    if course:
        _refresh_course_stats(db, course)
    db.commit()
    db.refresh(lesson)
    return {"ok": True, "item": _lesson_to_dict(lesson)}


@router.delete("/admin/columns/lessons/{lesson_id}")
def admin_delete_column_lesson(
    lesson_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    lesson = db.query(PracticeLesson).join(PracticeCourse).filter(
        PracticeLesson.id == lesson_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not lesson:
        raise HTTPException(404, "章节不存在")
    course = db.query(PracticeCourse).filter(PracticeCourse.id == lesson.course_id).first()
    db.delete(lesson)
    if course:
        _refresh_course_stats(db, course)
    db.commit()
    return {"ok": True}


@router.put("/admin/columns/lessons/reorder")
def admin_reorder_column_lessons(
    req: ColumnLessonReorderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    for item in req.items or []:
        lesson_id = int(item.get("id") or 0)
        sort_order = int(item.get("sort_order") or 0)
        lesson = db.query(PracticeLesson).join(PracticeCourse).filter(
            PracticeLesson.id == lesson_id,
            PracticeCourse.content_kind == "column",
        ).first()
        if lesson:
            lesson.sort_order = sort_order
            lesson.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/admin/columns/{column_id}/import-feishu")
async def admin_import_column_feishu(
    column_id: int,
    req: ColumnFeishuImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    course = db.query(PracticeCourse).filter(
        PracticeCourse.id == column_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not course:
        raise HTTPException(404, "专栏不存在")
    try:
        doc_id = parse_feishu_docx_id(req.url)
        imported = await import_feishu_docx(req.url, user_id=int(current_user.id))
    except FeishuImportError as exc:
        logger.warning("Feishu column import failed: %s", exc)
        raise HTTPException(400, str(exc))
    lesson = PracticeLesson(
        course_id=column_id,
        title=imported.title,
        description=_content_excerpt(imported.markdown),
        lesson_type="article",
        article_content=imported.markdown,
        video_duration=0,
        source_type="feishu",
        source_url=req.url.strip(),
        source_doc_id=doc_id,
        sort_order=req.sort_order if req.sort_order is not None else _next_lesson_sort(db, column_id),
        is_free=bool(req.is_free),
        is_published=bool(req.is_published),
    )
    db.add(lesson)
    db.flush()
    _refresh_course_stats(db, course)
    db.commit()
    db.refresh(lesson)
    return {
        "ok": True,
        "item": _lesson_to_dict(lesson),
        "title": imported.title,
        "image_count": imported.image_count,
        "pdf_count": imported.pdf_count,
        "style_count": imported.style_count,
        "callout_count": imported.callout_count,
        "warning_count": len(imported.warnings),
        "warnings": imported.warnings,
    }


@router.post("/admin/columns/lessons/{lesson_id}/sync-feishu")
async def admin_sync_column_feishu_lesson(
    lesson_id: int,
    req: ColumnFeishuSyncRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    lesson = db.query(PracticeLesson).join(PracticeCourse).filter(
        PracticeLesson.id == lesson_id,
        PracticeCourse.content_kind == "column",
    ).first()
    if not lesson:
        raise HTTPException(404, "章节不存在")
    source_url = (req.url or "").strip() or (lesson.source_url or "").strip()
    if not source_url:
        raise HTTPException(400, "这个章节还没有记录飞书链接")
    try:
        doc_id = parse_feishu_docx_id(source_url)
        imported = await import_feishu_docx(source_url, user_id=int(current_user.id))
    except FeishuImportError as exc:
        logger.warning("Feishu column lesson sync failed: %s", exc)
        raise HTTPException(400, str(exc))
    lesson.title = imported.title
    lesson.description = _content_excerpt(imported.markdown)
    lesson.lesson_type = "article"
    lesson.article_content = imported.markdown
    lesson.video_url = None
    lesson.video_duration = 0
    lesson.source_type = "feishu"
    lesson.source_url = source_url
    lesson.source_doc_id = doc_id
    lesson.updated_at = datetime.utcnow()
    course = db.query(PracticeCourse).filter(PracticeCourse.id == lesson.course_id).first()
    if course:
        _refresh_course_stats(db, course)
    db.commit()
    db.refresh(lesson)
    return {
        "ok": True,
        "item": _lesson_to_dict(lesson),
        "title": imported.title,
        "image_count": imported.image_count,
        "pdf_count": imported.pdf_count,
        "style_count": imported.style_count,
        "callout_count": imported.callout_count,
        "warning_count": len(imported.warnings),
        "warnings": imported.warnings,
    }


# ─── A1. 管理员内容列表（含未发布） ───
@router.get("/admin/projects")
def admin_list_projects(
    user_id: Optional[int] = Query(None),  # 兼容旧前端；后端以 JWT 管理员身份为准
    project_type: Optional[str] = Query(None),
    is_published: Optional[bool] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    conditions = ["1=1"]
    params = {}

    if project_type:
        conditions.append("p.project_type = :pt")
        params["pt"] = project_type
    if is_published is not None:
        conditions.append("p.is_published = :pub")
        params["pub"] = is_published
    if keyword:
        conditions.append("(p.title ILIKE :kw OR p.author_name ILIKE :kw)")
        params["kw"] = f"%{keyword}%"

    where = " AND ".join(conditions)
    total = db.execute(text(f"SELECT COUNT(*) FROM practice_projects p WHERE {where}"), params).scalar() or 0

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    rows = db.execute(text(f"""
        SELECT p.*,
          COALESCE((SELECT COUNT(*) FROM practice_likes l WHERE l.project_slug = p.slug), 0) as like_count,
          COALESCE((SELECT COUNT(*) FROM practice_comments c WHERE c.project_slug = p.slug), 0) as comment_count,
          COALESCE((SELECT COUNT(*) FROM practice_subscribes s WHERE s.project_slug = p.slug), 0) as subscribe_count
        FROM practice_projects p WHERE {where}
        ORDER BY p.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params).fetchall()

    sources = _load_feishu_sources()
    items = []
    for row in rows:
        item = row_to_dict(row)
        source_url = sources.get(item.get("slug") or "")
        item["can_sync_feishu"] = bool(source_url)
        item["feishu_source_url"] = source_url or None
        items.append(item)

    return {"total": total, "items": items}


# ─── A2. 标记/取消推荐 ───
class FeatureRequest(BaseModel):
    user_id: Optional[int] = None
    is_featured: bool

@router.put("/admin/projects/{slug}/feature")
def toggle_feature(
    slug: str,
    req: FeatureRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    db.execute(text("UPDATE practice_projects SET is_featured = :f WHERE slug = :slug"),
               {"f": req.is_featured, "slug": slug})
    db.commit()
    return {"ok": True, "is_featured": req.is_featured}


# ─── A3. 改项目状态 ───
class StatusRequest(BaseModel):
    user_id: Optional[int] = None
    status: str  # open / ongoing / ended / published

@router.put("/admin/projects/{slug}/status")
def change_status(
    slug: str,
    req: StatusRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    db.execute(text("UPDATE practice_projects SET status = :s, updated_at = NOW() WHERE slug = :slug"),
               {"s": req.status, "slug": slug})
    db.commit()
    return {"ok": True}


# ─── A4. 发布/下架 ───
class PublishToggleRequest(BaseModel):
    user_id: Optional[int] = None
    is_published: bool

@router.put("/admin/projects/{slug}/publish")
def toggle_publish(
    slug: str,
    req: PublishToggleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    db.execute(text("UPDATE practice_projects SET is_published = :p, updated_at = NOW() WHERE slug = :slug"),
               {"p": req.is_published, "slug": slug})
    db.commit()
    return {"ok": True}


class CoverUpdateRequest(BaseModel):
    user_id: Optional[int] = None
    cover_image: Optional[str] = None
    description: Optional[str] = None


@router.put("/admin/projects/{slug}/cover")
def update_project_cover(
    slug: str,
    req: CoverUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    cover_image = (req.cover_image or "").strip()
    description = (req.description or "").strip()[:500] or None
    if cover_image and not cover_image.startswith(("http://", "https://")):
        raise HTTPException(400, "封面图必须是 http(s) 图片链接")
    row = db.execute(
        text("SELECT id FROM practice_projects WHERE slug = :slug"),
        {"slug": slug},
    ).fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")
    db.execute(text("""
        UPDATE practice_projects
        SET cover_image = :cover_image,
            cover_color = :cover_color,
            description = :description,
            updated_at = NOW()
        WHERE slug = :slug
    """), {
        "cover_image": cover_image or None,
        "cover_color": "admin-cover" if cover_image else "#F5EFDE",
        "description": description,
        "slug": slug,
    })
    db.commit()
    return {"ok": True, "cover_image": cover_image or None, "description": description}


class FeishuSyncRequest(BaseModel):
    url: Optional[str] = None


@router.post("/admin/projects/{slug}/sync-feishu")
async def sync_feishu_project(
    slug: str,
    req: FeishuSyncRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    row = db.execute(
        text("SELECT id, md_filename FROM practice_projects WHERE slug = :slug"),
        {"slug": slug},
    ).fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")

    source_url = (req.url or "").strip() or _load_feishu_sources().get(slug, "")
    if not source_url:
        raise HTTPException(400, "这篇内容还没有记录飞书链接，请先粘贴原始飞书 docx 链接进行绑定同步")

    try:
        imported = await import_feishu_docx(source_url, user_id=int(current_user.id))
    except FeishuImportError as exc:
        logger.warning("Feishu project sync failed: %s", exc)
        raise HTTPException(400, str(exc))

    md_filename = os.path.basename(row[1] or f"{slug}.md")
    md_dir = _practice_md_dir()
    os.makedirs(md_dir, exist_ok=True)
    with open(os.path.join(md_dir, md_filename), "w", encoding="utf-8") as f:
        f.write(imported.markdown)

    db.execute(text("""
        UPDATE practice_projects
        SET title = :title,
            description = :description,
            md_filename = :md_filename,
            updated_at = NOW()
        WHERE slug = :slug
    """), {
        "title": imported.title,
        "description": _content_excerpt(imported.markdown),
        "md_filename": md_filename,
        "slug": slug,
    })
    db.commit()
    _set_feishu_source(slug, source_url)

    return {
        "ok": True,
        "slug": slug,
        "title": imported.title,
        "image_count": imported.image_count,
        "pdf_count": imported.pdf_count,
        "style_count": imported.style_count,
        "callout_count": imported.callout_count,
        "warning_count": len(imported.warnings),
        "warnings": imported.warnings,
    }


@router.delete("/admin/projects/{slug}")
def admin_delete_project(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    row = db.execute(
        text("SELECT id, md_filename FROM practice_projects WHERE slug = :slug"),
        {"slug": slug},
    ).fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")

    project_id = row[0]
    md_filename = row[1]
    db.execute(text("DELETE FROM practice_project_tags WHERE project_id = :pid"), {"pid": project_id})
    db.execute(text("DELETE FROM practice_likes WHERE project_slug = :slug"), {"slug": slug})
    db.execute(text("DELETE FROM practice_views WHERE project_slug = :slug"), {"slug": slug})
    db.execute(text("DELETE FROM practice_comments WHERE project_slug = :slug"), {"slug": slug})
    db.execute(text("DELETE FROM practice_subscribes WHERE project_slug = :slug"), {"slug": slug})
    db.execute(text("DELETE FROM practice_projects WHERE slug = :slug"), {"slug": slug})
    db.commit()
    _remove_feishu_source(slug)

    if md_filename:
        md_path = os.path.join(_practice_md_dir(), os.path.basename(md_filename))
        try:
            if os.path.exists(md_path):
                os.remove(md_path)
        except OSError:
            pass

    return {"ok": True}


# ─── A5. 管理后台统计 ───
@router.get("/admin/stats")
def admin_stats(
    user_id: Optional[int] = Query(None),  # 兼容旧前端；后端以 JWT 管理员身份为准
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin_user(current_user)
    projects = db.execute(text("SELECT COUNT(*) FROM practice_projects")).scalar()
    published = db.execute(text("SELECT COUNT(*) FROM practice_projects WHERE is_published = true")).scalar()
    featured = db.execute(text("SELECT COUNT(*) FROM practice_projects WHERE is_featured = true")).scalar()
    camps = db.execute(text("SELECT COUNT(*) FROM practice_projects WHERE project_type = 'camp'")).scalar()
    columns = db.execute(text("SELECT COUNT(*) FROM practice_courses WHERE content_kind = 'column'")).scalar()
    courses = db.execute(text("SELECT COUNT(*) FROM practice_courses WHERE COALESCE(content_kind, 'course') = 'course'")).scalar()
    lessons = db.execute(text("SELECT COUNT(*) FROM practice_lessons")).scalar()
    total_views = db.execute(text("SELECT COALESCE(SUM(view_count),0) FROM practice_projects")).scalar()
    total_likes = db.execute(text("SELECT COUNT(*) FROM practice_likes")).scalar()
    total_comments = db.execute(text("SELECT COUNT(*) FROM practice_comments")).scalar()
    total_subs = db.execute(text("SELECT COUNT(*) FROM practice_subscribes")).scalar()
    return {
        "projects": projects, "published": published, "featured": featured,
        "camps": camps, "columns": columns, "courses": courses, "lessons": lessons,
        "total_views": total_views, "total_likes": total_likes,
        "total_comments": total_comments, "total_subscribes": total_subs
    }


# ─── A6. 管理员登录验证 ───
class AdminLoginRequest(BaseModel):
    phone: str
    code: str

@router.post("/admin/login")
def admin_login(req: AdminLoginRequest, db: Session = Depends(get_db)):
    try:
        phone = verify_sms_code(db, req.phone, req.code, purpose="login")
    except SmsVerificationError as exc:
        raise HTTPException(400, str(exc))

    user_obj = db.query(User).filter(User.phone == phone).first()
    if not user_obj and is_default_admin_phone(phone):
        user_obj = User(phone=phone, created_at=datetime.utcnow(), is_admin=True)
        db.add(user_obj)
        db.commit()
        db.refresh(user_obj)
    if user_obj:
        user_obj = ensure_default_admin(user_obj, db)

    # 查用户
    user = db.execute(
        text("SELECT id, phone, nickname, is_admin FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()
    if not user:
        raise HTTPException(404, "用户不存在")
    if not user[3]:
        raise HTTPException(403, "非管理员账号")

    # 生成 token
    token = create_access_token(user[0])

    return {
        "ok": True,
        "token": token,
        "user_id": user[0],
        "phone": user[1],
        "nickname": user[2],
        "is_admin": True
    }


# ═══════════════════════════════════════
# Phase 2 管理 API
# ═══════════════════════════════════════

# ─── B1. 用户列表 ───
@router.get("/admin/users")
def admin_list_users(
    user_id: int = Query(...),
    keyword: Optional[str] = Query(None),
    plan: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30),
    db: Session = Depends(get_db),
):
    _check_admin(user_id, db)
    conditions = ["1=1"]
    params = {}

    if keyword:
        conditions.append("(u.phone ILIKE :kw OR u.nickname ILIKE :kw)")
        params["kw"] = f"%{keyword}%"

    where = " AND ".join(conditions)
    total_params = dict(params)
    if plan:
        if plan == 'none':
            plan_filter = """
                AND NOT EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.user_id = u.id AND m2.status = 'active'
                )
            """
        else:
            plan_filter = """
                AND EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.user_id = u.id
                      AND m2.status = 'active'
                      AND m2.plan_type ILIKE :plan_kw
                )
            """
            total_params["plan_kw"] = f"%{plan}%"
    else:
        plan_filter = ""

    total = db.execute(text(f"SELECT COUNT(*) FROM users u WHERE {where} {plan_filter}"), total_params).scalar() or 0

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    if plan and plan != 'none':
        params["plan_kw"] = f"%{plan}%"
    else:
        params["plan_kw"] = None

    rows = db.execute(text(f"""
        SELECT u.id, u.phone, u.nickname, u.avatar, u.created_at, u.is_active, u.is_admin,
               m.plan_type, m.expire_at, m.status as m_status
        FROM users u
        LEFT JOIN LATERAL (
            SELECT plan_type, expire_at, status
            FROM memberships
            WHERE user_id = u.id
              AND status = 'active'
              AND (:plan_kw IS NULL OR plan_type ILIKE :plan_kw)
            ORDER BY CASE
                WHEN plan_type ILIKE '%private%' THEN 1
                WHEN plan_type ILIKE '%year%' THEN 2
                WHEN plan_type ILIKE '%month%' THEN 3
                ELSE 4
            END, expire_at DESC NULLS LAST, id DESC
            LIMIT 1
        ) m ON true
        WHERE {where} {plan_filter}
        ORDER BY u.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params).fetchall()

    items = []
    for r in rows:
        d = {
            "id": r[0], "phone": r[1], "nickname": r[2], "avatar": r[3],
            "created_at": str(r[4]) if r[4] else None,
            "is_active": r[5], "is_admin": r[6],
            "plan_type": r[7], "expire_at": str(r[8]) if r[8] else None,
            "m_status": r[9]
        }
        items.append(d)

    return {"total": total, "items": items}


# ─── B2. 设置/取消管理员 ───
class AdminToggleRequest(BaseModel):
    user_id: int
    target_user_id: int
    is_admin: bool

@router.put("/admin/users/toggle-admin")
def toggle_admin(req: AdminToggleRequest, db: Session = Depends(get_db)):
    _check_admin(req.user_id, db)
    db.execute(text("UPDATE users SET is_admin = :v WHERE id = :uid"),
               {"v": req.is_admin, "uid": req.target_user_id})
    db.commit()
    return {"ok": True}


# ─── B3. 订阅列表 ───
@router.get("/admin/subscribes")
def admin_list_subscribes(
    user_id: int = Query(...),
    project_slug: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50),
    db: Session = Depends(get_db),
):
    _check_admin(user_id, db)
    conditions = ["1=1"]
    params = {}
    if project_slug:
        conditions.append("s.project_slug = :slug")
        params["slug"] = project_slug

    where = " AND ".join(conditions)
    total = db.execute(text(f"SELECT COUNT(*) FROM practice_subscribes s WHERE {where}"), params).scalar() or 0

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    rows = db.execute(text(f"""
        SELECT s.id, s.phone, s.project_slug, s.source_page, s.created_at,
               p.title as project_title
        FROM practice_subscribes s
        LEFT JOIN practice_projects p ON p.slug = s.project_slug
        WHERE {where}
        ORDER BY s.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params).fetchall()

    items = []
    for r in rows:
        items.append({
            "id": r[0], "phone": r[1], "project_slug": r[2],
            "source_page": r[3], "created_at": str(r[4]) if r[4] else None,
            "project_title": r[5]
        })

    return {"total": total, "items": items}


# ─── B4. 删除评论 ───
class DeleteCommentRequest(BaseModel):
    user_id: int

@router.delete("/admin/comments/{comment_id}")
def delete_comment(comment_id: int, req: DeleteCommentRequest, db: Session = Depends(get_db)):
    _check_admin(req.user_id, db)
    db.execute(text("DELETE FROM practice_comments WHERE id = :cid"), {"cid": comment_id})
    db.commit()
    return {"ok": True}


# ─── B5. 批量推荐/下架 ───
class BatchRequest(BaseModel):
    user_id: int
    slugs: list
    action: str  # feature / unfeature / publish / unpublish / delete

@router.post("/admin/batch")
def batch_action(req: BatchRequest, db: Session = Depends(get_db)):
    _check_admin(req.user_id, db)
    count = 0
    for slug in req.slugs:
        if req.action == 'feature':
            db.execute(text("UPDATE practice_projects SET is_featured = true WHERE slug = :s"), {"s": slug})
        elif req.action == 'unfeature':
            db.execute(text("UPDATE practice_projects SET is_featured = false WHERE slug = :s"), {"s": slug})
        elif req.action == 'publish':
            db.execute(text("UPDATE practice_projects SET is_published = true WHERE slug = :s"), {"s": slug})
        elif req.action == 'unpublish':
            db.execute(text("UPDATE practice_projects SET is_published = false WHERE slug = :s"), {"s": slug})
        elif req.action == 'delete':
            db.execute(text("UPDATE practice_projects SET is_published = false WHERE slug = :s"), {"s": slug})
        count += 1
    db.commit()
    return {"ok": True, "count": count}


# ═══════════════════════════════════════
# Phase 3 · 系统设置 API
# ═══════════════════════════════════════

# ─── C1. 获取所有设置（公开，前端读取） ───
@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT key, value FROM practice_settings")).fetchall()
    return {r[0]: r[1] for r in rows}


# ─── C2. 更新设置（管理员） ───
class SettingsUpdateRequest(BaseModel):
    user_id: int
    settings: dict  # {"key": "value", ...}

@router.put("/admin/settings")
def update_settings(req: SettingsUpdateRequest, db: Session = Depends(get_db)):
    _check_admin(req.user_id, db)
    for key, value in req.settings.items():
        db.execute(text("""
            INSERT INTO practice_settings (key, value, updated_at) VALUES (:k, :v, NOW())
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = NOW()
        """), {"k": key, "v": str(value)})
    db.commit()
    return {"ok": True}
