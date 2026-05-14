"""
Projects Router
==================
项目 = AI 工作空间（有自己的人设、模型、推荐问题）

API:
    GET    /workspace/projects              列出当前用户可见的所有项目（系统预设 + 自建）
    GET    /workspace/projects/{id}         项目详情
    POST   /workspace/projects              新建项目（用户自建）
    PATCH  /workspace/projects/{id}         更新项目
    DELETE /workspace/projects/{id}         删除项目（只能删自建的）
    POST   /workspace/projects/{id}/clone   把预设克隆一份到"我的项目"，然后可以改
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth import get_current_user
from chat_model_config import get_effective_default_model
from database import get_db
from models import Project, User
from permissions import get_active_plan, record_feature_usage, require_plan_feature

router = APIRouter(prefix="/workspace/projects", tags=["projects"])


# ═══════════════════════════════════════════════════════════════
#  Schemas
# ═══════════════════════════════════════════════════════════════

class ProjectOut(BaseModel):
    id: int
    user_id: Optional[int]
    name: str
    emoji: str
    tagline: Optional[str]
    system_prompt: Optional[str]
    model: str
    suggestions: Optional[list[str]]
    mode: str
    four_stage_preset: Optional[str]
    is_preset: bool
    is_home: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    emoji: str = Field(default="✨", max_length=16)
    tagline: Optional[str] = Field(default=None, max_length=200)
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    suggestions: Optional[list[str]] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    emoji: Optional[str] = Field(default=None, max_length=16)
    tagline: Optional[str] = Field(default=None, max_length=200)
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    suggestions: Optional[list[str]] = None
    sort_order: Optional[int] = None


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def _assert_visible(db: Session, user_id: int, project_id: int) -> Project:
    """项目必须是当前用户可见（自己的 or 系统预设）"""
    p = db.query(Project).filter(
        Project.id == project_id,
        or_(Project.user_id == user_id, Project.user_id.is_(None)),
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _assert_owned(db: Session, user_id: int, project_id: int) -> Project:
    """只允许修改自己创建的项目（预设不可改，要改要先 clone）"""
    p = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == user_id,
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found or not owned")
    return p


# ═══════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════

@router.get("", response_model=list[ProjectOut])
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户可见的项目：系统预设 + 用户自建，按 sort_order 排序"""
    rows = (
        db.query(Project)
        .filter(or_(Project.user_id == current_user.id, Project.user_id.is_(None)))
        .order_by(Project.is_preset.desc(), Project.sort_order, Project.id)
        .all()
    )
    return [ProjectOut.model_validate(p) for p in rows]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _assert_visible(db, current_user.id, project_id)
    return ProjectOut.model_validate(p)


@router.post("", response_model=ProjectOut)
def create_project(
    body: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_plan_feature(current_user, "custom_workspace", db)

    # Get max sort_order for this user
    last = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.sort_order.desc())
        .first()
    )
    sort_order = (last.sort_order + 1) if last else 0

    p = Project(
        user_id=current_user.id,
        name=body.name.strip(),
        emoji=body.emoji or "✨",
        tagline=body.tagline,
        system_prompt=body.system_prompt,
        model=body.model or get_effective_default_model(get_active_plan(current_user, db), db),
        suggestions=body.suggestions,
        mode="simple",  # 用户自建只能 simple 模式
        is_preset=False,
        is_home=False,
        sort_order=sort_order,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    record_feature_usage(current_user, "custom_workspace", db)
    return ProjectOut.model_validate(p)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    body: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _assert_owned(db, current_user.id, project_id)
    if body.name is not None:
        p.name = body.name.strip()
    if body.emoji is not None:
        p.emoji = body.emoji
    if body.tagline is not None:
        p.tagline = body.tagline
    if body.system_prompt is not None:
        p.system_prompt = body.system_prompt
    if body.model is not None:
        p.model = body.model
    if body.suggestions is not None:
        p.suggestions = body.suggestions
    if body.sort_order is not None:
        p.sort_order = body.sort_order
    db.commit()
    db.refresh(p)
    return ProjectOut.model_validate(p)


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _assert_owned(db, current_user.id, project_id)
    # 把该项目下的对话 project_id 清空（对话变成"未分类"，不丢失）
    from models import Conversation
    db.query(Conversation).filter(
        Conversation.project_id == project_id,
        Conversation.user_id == current_user.id,
    ).update({"project_id": None})
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.post("/{project_id}/clone", response_model=ProjectOut)
def clone_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把一个项目（通常是预设）克隆一份到当前用户的项目列表，然后可以改"""
    src = _assert_visible(db, current_user.id, project_id)
    require_plan_feature(current_user, "custom_workspace", db)

    last = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.sort_order.desc())
        .first()
    )
    sort_order = (last.sort_order + 1) if last else 0

    new = Project(
        user_id=current_user.id,
        name=src.name + "（副本）",
        emoji=src.emoji,
        tagline=src.tagline,
        system_prompt=src.system_prompt,
        model=src.model,
        suggestions=src.suggestions,
        mode="simple",  # 克隆后总是 simple，哪怕源是 four_stage
        is_preset=False,
        is_home=False,
        sort_order=sort_order,
    )
    db.add(new)
    db.commit()
    db.refresh(new)
    record_feature_usage(current_user, "custom_workspace", db)
    return ProjectOut.model_validate(new)
