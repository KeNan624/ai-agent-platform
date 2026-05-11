from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Enum, ForeignKey, Integer, Numeric, String, Boolean, Text, UniqueConstraint, text
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    nickname = Column(String(64), nullable=True)
    avatar = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    memberships = relationship("Membership", back_populates="user")
    credits = relationship("Credit", back_populates="user", uselist=False)
    orders = relationship("Order", back_populates="user")
    usage_logs = relationship("UsageLog", back_populates="user")


class Membership(Base):
    __tablename__ = "memberships"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    plan_type = Column(String(32), nullable=False)  # e.g. "free", "pro", "enterprise"
    start_at = Column(DateTime, nullable=False)
    expire_at = Column(DateTime, nullable=True)
    status = Column(
        Enum("active", "expired", "cancelled", name="membership_status"),
        default="active",
        nullable=False,
    )

    user = relationship("User", back_populates="memberships")


class Credit(Base):
    __tablename__ = "credits"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    balance = Column(Numeric(12, 2), default=0, nullable=False)
    total_bought = Column(Numeric(12, 2), default=0, nullable=False)
    total_used = Column(Numeric(12, 2), default=0, nullable=False)

    user = relationship("User", back_populates="credits")


class Order(Base):
    __tablename__ = "orders"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(32), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    pay_status = Column(
        Enum("pending", "paid", "failed", "refunded", name="pay_status"),
        default="pending",
        nullable=False,
    )
    trade_no = Column(String(128), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="orders")


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    model = Column(String(64), nullable=False)
    message_count = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="usage_logs")


# ═══════════════════════════════════════════════════════════════════════════
# Chat workspace · Folders + Conversations + Messages + Artifacts (Sprint 1)
# ═══════════════════════════════════════════════════════════════════════════

from sqlalchemy import JSON  # noqa: E402


class Folder(Base):
    """文件夹 · 对话分组的容器（纯归组，不影响 AI 人设）"""
    __tablename__ = "folders"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    emoji = Column(String(16), default="📁", nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    is_expanded = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Project(Base):
    """项目 · AI 工作空间（有自己的人设、模型、建议话题）

    两种来源：
      - 系统预设（is_preset=True, user_id=NULL）：所有用户可见
      - 用户自建（is_preset=False, user_id=xxx）：只有本人可见

    两种工作模式：
      - 'simple'     : 直接使用 system_prompt 跟 AI 对话
      - 'four_stage' : 走 plan/execute/reflect/recommend 4 阶段
                       配合 four_stage_preset 指定用 agent/prompts/ 里的哪套
    """
    __tablename__ = "projects"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)  # null = 系统预设
    name = Column(String(100), nullable=False)
    emoji = Column(String(16), default="✨", nullable=False)
    tagline = Column(String(200), nullable=True)
    system_prompt = Column(Text, nullable=True)
    model = Column(String(64), default="claude-sonnet-4-6", nullable=False)
    suggestions = Column(JSON, nullable=True)  # ["建议 1", "建议 2", ...]
    mode = Column(String(16), default="simple", nullable=False)  # 'simple' | 'four_stage'
    four_stage_preset = Column(String(32), nullable=True)  # e.g. 'side_hustle'
    is_preset = Column(Boolean, default=False, nullable=False)
    is_home = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Conversation(Base):
    """对话 · 一次聊天的容器，从属于一个项目，可归入文件夹"""
    __tablename__ = "conversations"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    project_id = Column(BigInteger, ForeignKey("projects.id"), nullable=True, index=True)
    folder_id = Column(BigInteger, ForeignKey("folders.id"), nullable=True, index=True)
    title = Column(String(200), default="新对话", nullable=False)
    # 保留 agent_type 做兼容（不再主用，只做 fallback 显示）
    agent_type = Column(String(32), default="side_hustle", nullable=False)
    pinned = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ChatMessage(Base):
    """消息 · 一条对话消息（重命名避免与 auth Message 冲突）

    批 1 新增 · 分支树
      - parent_message_id: 这条消息的"父" · NULL 表示对话的第一条
      - is_active_branch: 是否在当前激活路径上（true = 显示 / false = 沉入分支）

    示例：对同一条 user 消息，可能有多个 AI 回复（重新生成产生），
    它们都有同一个 parent_message_id，但只有其中 1 个 is_active_branch=true。
    """
    __tablename__ = "chat_messages"

    id = Column(BigInteger, primary_key=True, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # user / assistant / system
    content = Column(Text, nullable=False)
    attachments = Column(JSON, nullable=True)  # [{"type":"image", "url":"...", "filename":"..."}]
    artifacts = Column(JSON, nullable=True)    # AI 生成的产出（文章/图）
    message_metadata = Column(JSON, nullable=True)      # {"agent":"...", "model":"...", "plan":{...}, "tool_calls":[...]}
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # ⬇️ 批 1 新增 · 分支支持
    parent_message_id = Column(BigInteger, ForeignKey("chat_messages.id"), nullable=True, index=True)
    is_active_branch = Column(Boolean, default=True, nullable=False, index=True)


class Artifact(Base):
    """产出 · AI 生成的文章 / 图片 / 代码等（可单独归档）"""
    __tablename__ = "artifacts"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id"), nullable=True, index=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id"), nullable=True)
    type = Column(String(16), nullable=False)  # article / image / code / note
    title = Column(String(200), nullable=True)
    content = Column(Text, nullable=True)      # 正文 / 图片 URL / 代码
    artifact_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PptTaskToken(Base):
    """PPT 生成任务的服务端临时凭证，不通过 artifacts API 返回前端。"""
    __tablename__ = "ppt_task_tokens"

    artifact_id = Column(BigInteger, ForeignKey("artifacts.id"), primary_key=True)
    uid = Column(String(128), nullable=False, index=True)
    token = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ConversationSummary(Base):
    """对话滚动摘要 · 保存被压缩的早期上下文"""
    __tablename__ = "conversation_summaries"

    id = Column(BigInteger, primary_key=True, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id"), nullable=False, unique=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    summary = Column(Text, default="", nullable=False)
    summarized_message_count = Column(Integer, default=0, nullable=False)
    approx_tokens = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ConversationMemory(Base):
    """对话关键记忆 · 用户偏好 / 项目目标 / 长期设定"""
    __tablename__ = "conversation_memories"

    id = Column(BigInteger, primary_key=True, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id"), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    memory_type = Column(String(32), default="preference", nullable=False)
    content = Column(Text, nullable=False)
    confidence = Column(Integer, default=80, nullable=False)
    source = Column(String(32), default="manual", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ConversationContextStat(Base):
    """上下文构建统计 · 便于排查是否触发裁剪/摘要"""
    __tablename__ = "conversation_context_stats"

    id = Column(BigInteger, primary_key=True, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id"), nullable=False, unique=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    context_token_budget = Column(Integer, default=48000, nullable=False)
    approx_input_tokens = Column(Integer, default=0, nullable=False)
    raw_message_count = Column(Integer, default=0, nullable=False)
    sent_message_count = Column(Integer, default=0, nullable=False)
    summarized_message_count = Column(Integer, default=0, nullable=False)
    truncated_message_count = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ═══════════════════════════════════════════════════════════════
# 实战区项目 · Practice Projects
# ═══════════════════════════════════════════════════════════════

class PracticeProject(Base):
    """实战项目 · 卡片元信息存这里，详情正文存 backend/practice_md/{md_filename}

    project_type 区分两类：
      - 'camp'     · 营期项目：有开营时间、招募状态、需要预约（如「公众号冷启动·第一期」）
      - 'playbook' · 玩法库  ：长期复用的实战玩法（如「抖音 AI 数字人带货」）
    """
    __tablename__ = "practice_projects"

    id = Column(BigInteger, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)  # URL 标识，如 "gzh-cold-start"
    title = Column(String(200), nullable=False)                          # "公众号冷启动 · 第一期"
    description = Column(String(500), nullable=True)                     # 卡片上的一行描述
    project_type = Column(String(16), nullable=False, default="camp", index=True)  # camp | playbook
    # legacy 字段：之前的单标签，新版被 PracticeTag 多对多取代；保留是为了兼容旧数据/不破坏迁移
    category = Column(String(32), nullable=True, default=None)
    status = Column(String(32), nullable=False, default="upcoming")      # upcoming | recruiting | running | done | waitlist
    cover_emoji = Column(String(16), nullable=True)                      # "✍️" "🤖" "📱"
    cover_color = Column(String(16), nullable=True)                      # "1" | "2" | "3" 对应预设渐变
    cover_image = Column(Text, nullable=True)                            # 用户发布内容/后台封面图 URL
    md_filename = Column(String(128), nullable=True)                     # 指向 backend/practice_md/xxx.md
    sort_order = Column(Integer, default=100, nullable=False)            # 越小越靠前
    start_date = Column(String(64), nullable=True)                       # 显示用，"2026-05-15" 或 "待定"
    is_published = Column(Boolean, default=True, nullable=False)         # 是否公开（草稿用 false）
    is_featured = Column(Boolean, default=False, server_default=text("false"), nullable=False)  # 是否推荐到信息流
    view_count = Column(Integer, default=0, nullable=False)              # 浏览次数
    author_id = Column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
    author_name = Column(String(64), default="阿川", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # 多对多 · 标签
    tags = relationship("PracticeTag", secondary="practice_project_tags", back_populates="projects")


class PracticeTag(Base):
    """实战区标签字典 · 平台 / 项目类型 / 技能 三个维度共用同一张表，用 tag_type 区分"""
    __tablename__ = "practice_tags"

    id = Column(BigInteger, primary_key=True, index=True)
    name = Column(String(64), nullable=False, index=True)                # "抖音"、"电商"、"AI数字人"
    tag_type = Column(String(16), nullable=False, index=True)            # platform | category | skill
    sort_order = Column(Integer, default=100, nullable=False)            # 同维度内排序
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    projects = relationship("PracticeProject", secondary="practice_project_tags", back_populates="tags")


class PracticeProjectTag(Base):
    """项目 ↔ 标签 关联表"""
    __tablename__ = "practice_project_tags"

    project_id = Column(BigInteger, ForeignKey("practice_projects.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(BigInteger, ForeignKey("practice_tags.id", ondelete="CASCADE"), primary_key=True)


class PracticeSubscribe(Base):
    """实战营预约记录 · 用户留手机号预约开营通知"""
    __tablename__ = "practice_subscribes"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)  # 登录用户可关联
    phone = Column(String(20), nullable=False, index=True)
    project_slug = Column(String(64), nullable=True, index=True)         # 预约的具体项目，null 表示「全部上线都通知我」
    source_page = Column(String(32), nullable=True)                      # "list" | "detail" 区分预约来源
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    notified_at = Column(DateTime, nullable=True)                        # 通知发送时间（开营时填）


class PracticeLike(Base):
    """实战区点赞记录 · 每个用户对同一项目只能点赞一次"""
    __tablename__ = "practice_likes"
    __table_args__ = (UniqueConstraint("user_id", "project_slug", name="uq_practice_likes_user_project"),)

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    project_slug = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PracticeComment(Base):
    """实战区评论"""
    __tablename__ = "practice_comments"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    project_slug = Column(String(64), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PracticeView(Base):
    """实战区浏览记录 · 用于按用户去重"""
    __tablename__ = "practice_views"
    __table_args__ = (UniqueConstraint("user_id", "project_slug", name="uq_practice_views_user_project"),)

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    project_slug = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PracticeSetting(Base):
    """实战区后台设置"""
    __tablename__ = "practice_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AppSetting(Base):
    """后台运行时配置 · 用于 API Key / Base URL 等全局设置"""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SmsVerificationCode(Base):
    """短信验证码记录 · 发送、过期、重试和消费状态都落库"""
    __tablename__ = "sms_verification_codes"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    phone = Column(String(20), nullable=False, index=True)
    purpose = Column(String(32), default="login", nullable=False, index=True)
    code_hash = Column(String(128), nullable=False)
    provider = Column(String(32), default="smsbao", nullable=False)
    status = Column(String(24), default="created", nullable=False, index=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    client_ip = Column(String(64), nullable=True, index=True)
    provider_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    sent_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    verified_at = Column(DateTime, nullable=True)


# ═══════════════════════════════════════════════════════════════
# Batch 7 · 视频课程
# ═══════════════════════════════════════════════════════════════

class PracticeCourse(Base):
    __table_args__ = {"extend_existing": True}
    """视频课程/专栏"""
    __tablename__ = "practice_courses"

    id = Column(BigInteger, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    cover_url = Column(String(500), nullable=True)
    cover_emoji = Column(String(16), nullable=True)
    category = Column(String(32), nullable=True, index=True)
    instructor = Column(String(64), nullable=True)
    content_kind = Column(String(16), default="course", nullable=False, index=True)  # course / column
    price = Column(Numeric(10, 2), default=0, nullable=False)
    status = Column(String(16), default="draft", nullable=False, index=True)
    sort_order = Column(Integer, default=0, nullable=False)
    lesson_count = Column(Integer, default=0, nullable=False)
    total_duration = Column(Integer, default=0, nullable=False)
    view_count = Column(Integer, default=0, nullable=False)
    is_published = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    lessons = relationship("PracticeLesson", back_populates="course", order_by="PracticeLesson.sort_order")


class PracticeLesson(Base):
    __table_args__ = {"extend_existing": True}
    """课时"""
    __tablename__ = "practice_lessons"

    id = Column(BigInteger, primary_key=True, index=True)
    course_id = Column(BigInteger, ForeignKey("practice_courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    lesson_type = Column(String(16), default="video", nullable=False)
    video_url = Column(String(500), nullable=True)
    video_duration = Column(Integer, default=0, nullable=False)
    article_content = Column(Text, nullable=True)
    source_type = Column(String(32), nullable=True)  # feishu / manual
    source_url = Column(Text, nullable=True)
    source_doc_id = Column(String(128), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False, index=True)
    is_free = Column(Boolean, default=False, nullable=False)
    is_published = Column(Boolean, default=True, nullable=False)
    view_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    course = relationship("PracticeCourse", back_populates="lessons")


# ═══════════════════════════════════════════════════════════════
# Batch 7 · 视频课程
# ═══════════════════════════════════════════════════════════════
