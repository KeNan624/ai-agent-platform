"""
Conversations & Folders Router
================================
对话 + 文件夹 + 消息的完整 CRUD

API 设计：
    GET    /workspace/tree              获取当前用户的完整工作区（文件夹 + 对话）
    POST   /workspace/folders           新建文件夹
    PATCH  /workspace/folders/{id}      更新文件夹（重命名、换 emoji、展开/折叠）
    DELETE /workspace/folders/{id}      删除文件夹（里面的对话变为未分组）

    POST   /workspace/conversations                     新建对话
    GET    /workspace/conversations/{id}                获取对话详情（含所有消息）
    PATCH  /workspace/conversations/{id}                更新对话（标题 / 文件夹 / 置顶 / agent）
    DELETE /workspace/conversations/{id}                删除对话
    POST   /workspace/conversations/{id}/messages       追加一条消息
    POST   /workspace/conversations/{id}/auto-title     让 AI 基于前几条消息自动命名
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth import get_current_user
from auth import ALGORITHM, SECRET_KEY
from database import get_db
from models import (
    ChatMessage,
    Conversation,
    ConversationContextStat,
    ConversationMemory,
    ConversationSummary,
    Folder,
    User,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])


# ═══════════════════════════════════════════════════════════════════════
#  Schemas
# ═══════════════════════════════════════════════════════════════════════

class FolderCreate(BaseModel):
    name: str
    emoji: Optional[str] = "📁"


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    is_expanded: Optional[bool] = None
    sort_order: Optional[int] = None


class FolderOut(BaseModel):
    id: int
    name: str
    emoji: str
    is_expanded: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    title: Optional[str] = "新对话"
    agent_type: Optional[str] = "simple"
    folder_id: Optional[int] = None
    project_id: Optional[int] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    agent_type: Optional[str] = None
    folder_id: Optional[int] = None
    move_out_of_folder: Optional[bool] = None
    project_id: Optional[int] = None
    move_out_of_project: Optional[bool] = None
    pinned: Optional[bool] = None
    sort_order: Optional[int] = None


class ConversationBrief(BaseModel):
    id: int
    title: str
    agent_type: str
    folder_id: Optional[int]
    project_id: Optional[int] = None
    pinned: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    message_count: int = 0

    class Config:
        from_attributes = True


class ConversationShareOut(BaseModel):
    ok: bool
    share_id: str
    url: str


class MessageCreate(BaseModel):
    role: str  # user / assistant
    content: str
    attachments: Optional[list[dict]] = None
    artifacts: Optional[list[dict]] = None
    metadata: Optional[dict] = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    attachments: Optional[list[dict]]
    artifacts: Optional[list[dict]]
    metadata: Optional[dict]
    created_at: datetime
    # ⬇️ 批 1 · 分支信息
    parent_message_id: Optional[int] = None
    is_active_branch: bool = True
    # 同 parent 下兄弟消息总数（前端渲染 ‹1/2› 用）· 1 = 无分支
    sibling_count: int = 1
    # 当前是兄弟中的第几个（按 created_at 顺序，1-based）
    sibling_index: int = 1

    class Config:
        from_attributes = True


class ConversationDetail(ConversationBrief):
    messages: list[MessageOut] = []


class WorkspaceTree(BaseModel):
    folders: list[FolderOut]
    conversations_by_folder: dict[str, list[ConversationBrief]]  # folder_id(str) -> list, "null" = unfiled


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def _message_to_out(m: ChatMessage, sibling_count: int = 1, sibling_index: int = 1) -> MessageOut:
    return MessageOut(
        id=m.id,
        role=m.role,
        content=m.content,
        attachments=m.attachments,
        artifacts=m.artifacts,
        metadata=m.message_metadata,
        created_at=m.created_at,
        parent_message_id=m.parent_message_id,
        is_active_branch=m.is_active_branch,
        sibling_count=sibling_count,
        sibling_index=sibling_index,
    )


def _build_active_branch(db: Session, conv_id: int) -> list[tuple[ChatMessage, int, int]]:
    """构建当前对话的"激活分支路径" · 返 [(message, sibling_count, sibling_index), ...]

    算法：
    1. 查全部 is_active_branch=true 的消息
    2. 按 parent_message_id 还原父子链路，输出根节点 -> 叶子节点
    3. 对每条消息，查同 parent_message_id 的兄弟总数 + 自己的索引
    """
    active_msgs = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conv_id,
            ChatMessage.is_active_branch == True,  # noqa: E712
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )

    # 预查所有兄弟分组的计数（一次性查，避免 N+1）
    all_msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )
    # parent_id -> list of msg（同 parent = 兄弟）
    by_parent: dict = {}
    for m in all_msgs:
        key = m.parent_message_id  # 可能是 None（根节点兄弟组）
        by_parent.setdefault(key, []).append(m)

    active_by_id = {m.id: m for m in active_msgs}
    active_children: dict[Optional[int], list[ChatMessage]] = {}
    for m in active_msgs:
        active_children.setdefault(m.parent_message_id, []).append(m)
    for children in active_children.values():
        children.sort(key=lambda item: (item.created_at, item.id))

    roots = [
        m for m in active_msgs
        if m.parent_message_id is None or m.parent_message_id not in active_by_id
    ]
    roots.sort(key=lambda item: (item.created_at, item.id))

    ordered_msgs: list[ChatMessage] = []
    visited: set[int] = set()

    def walk(node: ChatMessage):
        if node.id in visited:
            return
        visited.add(node.id)
        ordered_msgs.append(node)
        for child in active_children.get(node.id, []):
            walk(child)

    for root in roots:
        walk(root)
    for m in active_msgs:
        if m.id not in visited:
            ordered_msgs.append(m)

    result = []
    for m in ordered_msgs:
        siblings = by_parent.get(m.parent_message_id, [])
        sib_count = len(siblings)
        # 找自己在兄弟中的位置（按 created_at 顺序）
        sib_index = 1
        for idx, s in enumerate(siblings, start=1):
            if s.id == m.id:
                sib_index = idx
                break
        result.append((m, sib_count, sib_index))
    return result


def _conv_to_brief(c: Conversation, message_count: int = 0) -> ConversationBrief:
    return ConversationBrief(
        id=c.id,
        title=c.title,
        agent_type=c.agent_type,
        folder_id=c.folder_id,
        project_id=c.project_id,
        pinned=c.pinned,
        sort_order=c.sort_order,
        created_at=c.created_at,
        updated_at=c.updated_at,
        message_count=message_count,
    )


def _assert_own_folder(db: Session, user_id: int, folder_id: int) -> Folder:
    f = db.query(Folder).filter(Folder.id == folder_id, Folder.user_id == user_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Folder not found")
    return f


def _assert_own_conversation(db: Session, user_id: int, conv_id: int) -> Conversation:
    c = db.query(Conversation).filter(
        Conversation.id == conv_id, Conversation.user_id == user_id
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return c


# ═══════════════════════════════════════════════════════════════════════
#  Workspace tree (left sidebar data)
# ═══════════════════════════════════════════════════════════════════════

@router.get("/tree", response_model=WorkspaceTree)
def get_workspace_tree(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户的完整工作区：文件夹 + 其下对话 + 未分组对话"""
    folders = (
        db.query(Folder)
        .filter(Folder.user_id == current_user.id)
        .order_by(Folder.sort_order, Folder.created_at)
        .all()
    )

    all_convs = (
        db.query(Conversation)
        .filter(Conversation.user_id == current_user.id)
        .order_by(desc(Conversation.pinned), desc(Conversation.updated_at))
        .all()
    )

    # Count messages per conversation (one query)
    conv_ids = [c.id for c in all_convs]
    msg_counts: dict[int, int] = {}
    if conv_ids:
        from sqlalchemy import func
        rows = (
            db.query(ChatMessage.conversation_id, func.count(ChatMessage.id))
            .filter(ChatMessage.conversation_id.in_(conv_ids))
            .group_by(ChatMessage.conversation_id)
            .all()
        )
        msg_counts = {cid: cnt for cid, cnt in rows}

    grouped: dict[str, list[ConversationBrief]] = {}
    for c in all_convs:
        key = str(c.folder_id) if c.folder_id else "null"
        grouped.setdefault(key, []).append(_conv_to_brief(c, msg_counts.get(c.id, 0)))

    return WorkspaceTree(
        folders=[FolderOut.model_validate(f) for f in folders],
        conversations_by_folder=grouped,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Folders CRUD
# ═══════════════════════════════════════════════════════════════════════

@router.post("/folders", response_model=FolderOut)
def create_folder(
    body: FolderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Get max sort_order
    last = (
        db.query(Folder)
        .filter(Folder.user_id == current_user.id)
        .order_by(desc(Folder.sort_order))
        .first()
    )
    sort_order = (last.sort_order + 1) if last else 0

    folder = Folder(
        user_id=current_user.id,
        name=body.name.strip()[:100] or "新文件夹",
        emoji=body.emoji or "📁",
        sort_order=sort_order,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return FolderOut.model_validate(folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: int,
    body: FolderUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    folder = _assert_own_folder(db, current_user.id, folder_id)
    if body.name is not None:
        folder.name = body.name.strip()[:100]
    if body.emoji is not None:
        folder.emoji = body.emoji
    if body.is_expanded is not None:
        folder.is_expanded = body.is_expanded
    if body.sort_order is not None:
        folder.sort_order = body.sort_order
    db.commit()
    db.refresh(folder)
    return FolderOut.model_validate(folder)


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    folder = _assert_own_folder(db, current_user.id, folder_id)
    # Move conversations out (set folder_id = null), don't delete them
    db.query(Conversation).filter(
        Conversation.folder_id == folder_id,
        Conversation.user_id == current_user.id,
    ).update({"folder_id": None})
    db.delete(folder)
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
#  Conversations CRUD
# ═══════════════════════════════════════════════════════════════════════

@router.post("/conversations", response_model=ConversationBrief)
def create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Validate folder belongs to user if provided
    if body.folder_id:
        _assert_own_folder(db, current_user.id, body.folder_id)
    # Validate project is visible to user if provided
    if body.project_id:
        from models import Project
        from sqlalchemy import or_
        p = db.query(Project).filter(
            Project.id == body.project_id,
            or_(Project.user_id == current_user.id, Project.user_id.is_(None)),
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found")

    conv = Conversation(
        user_id=current_user.id,
        title=body.title or "新对话",
        agent_type=body.agent_type or "simple",
        folder_id=body.folder_id,
        project_id=body.project_id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return _conv_to_brief(conv, 0)


@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
def get_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返当前对话的"激活分支" · 默认只显示 is_active_branch=true 的消息

    前端可以通过每条消息的 sibling_count/sibling_index 知道"这里还有 N 个旧版本"，
    用户点‹›切换时调 /messages/{id}/switch 激活那条分支。
    """
    conv = _assert_own_conversation(db, current_user.id, conv_id)
    branch_tuples = _build_active_branch(db, conv_id)
    # 消息总数（所有分支）· 用于 ConversationBrief.message_count
    total_count = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id)
        .count()
    )
    result = _conv_to_brief(conv, total_count)
    return ConversationDetail(
        **result.model_dump(),
        messages=[_message_to_out(m, sc, si) for (m, sc, si) in branch_tuples],
    )


@router.patch("/conversations/{conv_id}", response_model=ConversationBrief)
def update_conversation(
    conv_id: int,
    body: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _assert_own_conversation(db, current_user.id, conv_id)
    if body.title is not None:
        conv.title = body.title.strip()[:200] or "未命名对话"
    if body.agent_type is not None:
        conv.agent_type = body.agent_type
    if body.pinned is not None:
        conv.pinned = body.pinned
    if body.sort_order is not None:
        conv.sort_order = body.sort_order
    if body.move_out_of_folder:
        conv.folder_id = None
    elif body.folder_id is not None:
        _assert_own_folder(db, current_user.id, body.folder_id)
        conv.folder_id = body.folder_id
    if body.move_out_of_project:
        conv.project_id = None
    elif body.project_id is not None:
        from models import Project
        from sqlalchemy import or_
        p = db.query(Project).filter(
            Project.id == body.project_id,
            or_(Project.user_id == current_user.id, Project.user_id.is_(None)),
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found")
        conv.project_id = body.project_id
    db.commit()
    db.refresh(conv)
    msg_count = db.query(ChatMessage).filter(ChatMessage.conversation_id == conv.id).count()
    return _conv_to_brief(conv, msg_count)


@router.delete("/conversations/{conv_id}")
def delete_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _assert_own_conversation(db, current_user.id, conv_id)
    db.query(ConversationContextStat).filter(ConversationContextStat.conversation_id == conv_id).delete()
    db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conv_id).delete()
    db.query(ConversationSummary).filter(ConversationSummary.conversation_id == conv_id).delete()
    db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id).delete()
    db.delete(conv)
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
#  Messages
# ═══════════════════════════════════════════════════════════════════════

@router.post("/conversations/{conv_id}/messages", response_model=MessageOut)
def append_message(
    conv_id: int,
    body: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """追加消息 · 自动链到当前激活分支末尾。

    批 1 新增 · 分支树：
      - 如果对话有激活消息，新消息的 parent_message_id = 最后一条激活消息的 id
      - 新消息默认 is_active_branch=True（进入激活分支尾部）
    """
    conv = _assert_own_conversation(db, current_user.id, conv_id)
    # 找激活分支的最后一条消息作为 parent
    last_active = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conv.id,
            ChatMessage.is_active_branch == True,  # noqa: E712
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    parent_id = last_active.id if last_active else None

    msg = ChatMessage(
        conversation_id=conv.id,
        role=body.role,
        content=body.content,
        attachments=body.attachments,
        artifacts=body.artifacts,
        message_metadata=body.metadata,
        parent_message_id=parent_id,
        is_active_branch=True,
    )
    db.add(msg)
    conv.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(msg)
    # 新消息刚创建，默认唯一兄弟，sibling_count=1 index=1
    return _message_to_out(msg, 1, 1)


# ═══════════════════════════════════════════════════════════════════════
#  批 1 · 分支操作（编辑消息 / 重新生成 / 切换版本）
# ═══════════════════════════════════════════════════════════════════════

class EditMessageBody(BaseModel):
    content: str


def _deactivate_descendants(db: Session, msg_id: int):
    """递归把一条消息及其所有子孙标记为 is_active_branch=False

    BFS 做法，避免递归栈爆炸。
    """
    to_deactivate = [msg_id]
    visited = set()
    while to_deactivate:
        cur_id = to_deactivate.pop()
        if cur_id in visited:
            continue
        visited.add(cur_id)
        cur = db.query(ChatMessage).filter(ChatMessage.id == cur_id).first()
        if cur:
            cur.is_active_branch = False
        # 找所有 parent=cur_id 的子节点
        children = (
            db.query(ChatMessage)
            .filter(ChatMessage.parent_message_id == cur_id)
            .all()
        )
        for ch in children:
            to_deactivate.append(ch.id)


def _ancestor_chain(db: Session, message: ChatMessage) -> list[ChatMessage]:
    """Return root -> message for the selected branch target."""
    chain: list[ChatMessage] = []
    current: Optional[ChatMessage] = message
    while current is not None:
        chain.append(current)
        if current.parent_message_id is None:
            break
        current = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.id == current.parent_message_id,
                ChatMessage.conversation_id == message.conversation_id,
            )
            .first()
        )
    return list(reversed(chain))


def _preferred_descendant_chain(db: Session, start_id: int) -> list[ChatMessage]:
    """Pick one saved continuation below start_id, preferring the active/newest child."""
    chain: list[ChatMessage] = []
    current_id = start_id
    while True:
        child = (
            db.query(ChatMessage)
            .filter(ChatMessage.parent_message_id == current_id)
            .order_by(
                desc(ChatMessage.is_active_branch),
                desc(ChatMessage.created_at),
                desc(ChatMessage.id),
            )
            .first()
        )
        if not child:
            break
        chain.append(child)
        current_id = child.id
    return chain


def _activate_branch_path(db: Session, target: ChatMessage) -> list[int]:
    """Activate a full ChatGPT-style branch path for target."""
    active_messages = _ancestor_chain(db, target) + _preferred_descendant_chain(db, target.id)
    active_ids = [m.id for m in active_messages]

    db.query(ChatMessage).filter(ChatMessage.conversation_id == target.conversation_id).update(
        {ChatMessage.is_active_branch: False},
        synchronize_session=False,
    )
    if active_ids:
        db.query(ChatMessage).filter(ChatMessage.id.in_(active_ids)).update(
            {ChatMessage.is_active_branch: True},
            synchronize_session=False,
        )
    return active_ids


@router.post("/messages/{msg_id}/edit", response_model=MessageOut)
def edit_message(
    msg_id: int,
    body: EditMessageBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """编辑用户消息 · 在同 parent 下创建新兄弟消息，旧的及其子孙沉入分支

    只能编辑 role=user 的消息。
    AI 回复的"重新生成"请用 /messages/{id}/regenerate 接口。
    """
    old = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not old:
        raise HTTPException(404, "消息不存在")
    # 权限：消息所属对话必须是当前用户的
    _assert_own_conversation(db, current_user.id, old.conversation_id)

    if old.role != "user":
        raise HTTPException(400, "只能编辑用户消息（AI 回复请用 regenerate）")

    # 沉入旧消息及所有子孙
    _deactivate_descendants(db, msg_id)

    # 新消息：同 parent，内容是编辑后的
    new_msg = ChatMessage(
        conversation_id=old.conversation_id,
        role="user",
        content=body.content,
        parent_message_id=old.parent_message_id,  # ← 关键：同 parent
        is_active_branch=True,
        attachments=old.attachments,  # 附件保留（后续可以改）
    )
    db.add(new_msg)
    db.commit()
    db.refresh(new_msg)

    # 计算新消息的 sibling 信息
    siblings = (
        db.query(ChatMessage)
        .filter(ChatMessage.parent_message_id == old.parent_message_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )
    sib_count = len(siblings)
    sib_index = next((i for i, s in enumerate(siblings, 1) if s.id == new_msg.id), sib_count)

    return _message_to_out(new_msg, sib_count, sib_index)


@router.post("/messages/{msg_id}/regenerate")
def regenerate_message(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """准备重新生成 AI 回复 · 沉掉旧 AI 消息 + 它的子孙

    只做 DB 改动，不触发 AI 生成。AI 生成由前端接下来调 /chat/stream 处理。
    返回：{ok, parent_message_id} · 前端用 parent_message_id 去存新 AI 回复
    """
    old = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not old:
        raise HTTPException(404, "消息不存在")
    _assert_own_conversation(db, current_user.id, old.conversation_id)

    if old.role != "assistant":
        raise HTTPException(400, "只能对 AI 回复重新生成")

    # 沉入旧 AI 消息及所有子孙
    _deactivate_descendants(db, msg_id)
    db.commit()

    return {
        "ok": True,
        "parent_message_id": old.parent_message_id,
        "conversation_id": old.conversation_id,
    }


@router.post("/messages/{msg_id}/switch-branch")
def switch_branch(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """切换到指定消息这个分支 · 恢复它的祖先、自己和已有后续。"""
    target = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not target:
        raise HTTPException(404, "消息不存在")
    _assert_own_conversation(db, current_user.id, target.conversation_id)

    active_ids = _activate_branch_path(db, target)
    db.commit()
    return {"ok": True, "active_message_id": msg_id, "active_message_ids": active_ids}


@router.get("/messages/{msg_id}/siblings", response_model=list[MessageOut])
def get_siblings(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回某条消息的所有"兄弟消息"（同 parent_message_id 的）· 按 created_at 升序

    前端做版本切换 ‹ X/Y › 时，需要知道所有版本的 id 才能调 switch-branch。
    """
    target = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not target:
        raise HTTPException(404, "消息不存在")
    _assert_own_conversation(db, current_user.id, target.conversation_id)

    siblings = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == target.conversation_id,
            ChatMessage.parent_message_id == target.parent_message_id,
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )
    total = len(siblings)
    return [_message_to_out(s, total, idx) for idx, s in enumerate(siblings, start=1)]

_MAX_TITLE_CHARS = 30  # 上限 30 字
_DEFAULT_TITLES = ("新对话", "未命名对话", "")


def _is_auto_title_allowed(title: str | None) -> bool:
    """只有默认标题或明显错误的占位标题才允许自动覆盖。"""
    raw = (title or "").strip()
    if raw in _DEFAULT_TITLES:
        return True
    # 前端旧 bug 会把隐藏输入框残留的 2222 / 3333 写成标题。
    if raw.isdigit() and len(raw) <= 8:
        return True
    return False


@router.post("/conversations/{conv_id}/auto-title")
async def auto_title(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """根据第一条用户消息生成对话标题。

    规则：
      - 仅当当前标题是默认值（"新对话"/"未命名对话"/空）时才覆盖
      - 用户已经手动改过名字 → 永远不覆盖
      - 标题取自第一条 role=user 的消息，截断 30 字
      - 跳过 [[MEDIA]] 系媒体消息（这些不是用户输入的文字）
    """
    conv = _assert_own_conversation(db, current_user.id, conv_id)

    # 用户已经改过 → 不动；默认标题 / 纯数字占位标题可自动覆盖。
    if not _is_auto_title_allowed(conv.title):
        return {"title": conv.title, "changed": False}

    # 找第一条用户消息（user 角色不会是 [[MEDIA]] 标记）
    first_user_msg = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conv_id,
            ChatMessage.role == "user",
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .first()
    )

    if not first_user_msg or not (first_user_msg.content or "").strip():
        return {"title": conv.title, "changed": False}

    text = (first_user_msg.content or "").strip()
    # 取第一行（避免多行消息整段塞进标题）
    first_line = text.split("\n", 1)[0].strip()
    new_title = first_line[:_MAX_TITLE_CHARS] or "新对话"

    if new_title == conv.title:
        return {"title": conv.title, "changed": False}

    conv.title = new_title
    db.commit()
    db.refresh(conv)
    return {"title": conv.title, "changed": True}


@router.post("/conversations/{conv_id}/share", response_model=ConversationShareOut)
def share_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _assert_own_conversation(db, current_user.id, conv_id)
    token = jwt.encode(
        {
            "type": "conversation_share",
            "conv_id": conv.id,
            "user_id": current_user.id,
            "exp": datetime.utcnow() + timedelta(days=30),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return ConversationShareOut(
        ok=True,
        share_id=token,
        url=f"/share/{token}",
    )


# =============================================================================
# 🎬 v25 · 历史回放修复 · conversations.py 增加片段
# =============================================================================
# 用途:让前端能在视频任务完成后 · 把 video_url 写回 ChatMessage.attachments
#
# 部署方法(用文本编辑器在 conversations.py 末尾粘贴):
#   1. 用文本编辑器打开 ~/Projects/ai-agent-platform/routers/conversations.py
#   2. 文件末尾粘贴下面 ===== 之间的代码
#   3. 保存
#
# 或者跑 bash 命令一键追加:
#   cat ~/Downloads/conversations_v25_append.py >> ~/Projects/ai-agent-platform/routers/conversations.py
# =============================================================================

# ========== 复制下面到 conversations.py 文件末尾 ==========

from pydantic import BaseModel as _BaseModel_v25  # 防止重复导入冲突


class _AttachmentPatchBody_v25(_BaseModel_v25):
    """v25 · 给某条 message 的 attachments 数组里某个条目补字段(主要是给视频补 video_url)"""
    task_id: str           # 在 attachments 里找哪个条目(只对 type=video 用)
    patch: dict            # 要 merge 的字段 · 比如 {"video_url": "...", "status": "done"}


@router.patch("/messages/{msg_id}/attachment")
def patch_message_attachment(
    msg_id: int,
    body: _AttachmentPatchBody_v25,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """v25 · 视频任务完成时 · 前端调这个接口把 video_url 写回 message。

    流程:
      1. 前端拿到 message_id(写 AI message 时后端返回)
      2. 前端把 attachments=[{type:'video', task_id:'xxx', ...}] 也写进 AI message
      3. 前端轮询 /media/video/task/{task_id} 拿到 video_url 后
      4. 调本接口 patch attachments[找到 task_id 的那个].video_url = 真实 url
    """
    msg = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    # 校验对话归属
    conv = db.query(Conversation).filter(Conversation.id == msg.conversation_id).first()
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your message")

    # 找到对应 task_id 的条目 · 合并 patch
    atts = list(msg.attachments or [])
    found = False
    for i, att in enumerate(atts):
        if isinstance(att, dict) and att.get("task_id") == body.task_id:
            new_att = {**att, **(body.patch or {})}
            atts[i] = new_att
            found = True
            break
    if not found:
        # 找不到对应 task_id · 不报错 · 静默(可能是孤儿任务)
        return {"ok": False, "reason": "task_id not found in attachments"}

    msg.attachments = atts
    # SQLAlchemy 对 JSON 字段需要标记 dirty 才会真的写回
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(msg, "attachments")
    db.commit()
    return {"ok": True, "attachment": atts[i]}


# ========== 复制截止 ==========
