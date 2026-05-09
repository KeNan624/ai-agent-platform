from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import ConversationContextStat, ConversationMemory, ConversationSummary


CONTEXT_TOKEN_BUDGET = int(os.getenv("CONVERSATION_CONTEXT_TOKENS", "48000"))
CONTEXT_RESERVED_OUTPUT_TOKENS = int(os.getenv("CONVERSATION_RESERVED_OUTPUT_TOKENS", "8000"))
CONTEXT_RECENT_MESSAGES = int(os.getenv("CONVERSATION_RECENT_MESSAGES", "80"))
SUMMARY_MAX_CHARS = int(os.getenv("CONVERSATION_SUMMARY_MAX_CHARS", "6000"))


@dataclass
class ContextBuildResult:
    history: list[dict[str, Any]]
    approx_input_tokens: int
    raw_message_count: int
    sent_message_count: int
    summarized_message_count: int
    truncated_message_count: int
    context_token_budget: int


def estimate_tokens(value: Any) -> int:
    """粗略 token 估算，偏保守，避免中文长对话把上游上下文打爆。"""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = " ".join(_content_part_to_text(part) for part in value)
    else:
        text = str(value)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = max(len(text) - cjk, 0)
    return max(1, math.ceil(cjk * 0.9 + other / 3.8))


def build_conversation_context(
    *,
    db: Session,
    user_id: int,
    conversation_id: int | None,
    history: list[dict[str, Any]],
    current_user_message: str = "",
) -> ContextBuildResult:
    raw_history = []
    for item in history or []:
        normalized = _normalize_message(item)
        if normalized:
            raw_history.append(normalized)
    memories = _load_memories(db, user_id, conversation_id)

    recent_messages = raw_history[-CONTEXT_RECENT_MESSAGES:]
    older_messages = raw_history[:-CONTEXT_RECENT_MESSAGES]
    summary_text = ""
    if older_messages:
        summary_text = _build_extractive_summary(older_messages)
        _upsert_summary(db, user_id, conversation_id, summary_text, len(older_messages))
    else:
        summary_text = _load_summary(db, user_id, conversation_id)

    packed: list[dict[str, Any]] = []
    prelude = _build_prelude(summary_text, memories)
    if prelude:
        packed.extend([
            {"role": "user", "content": prelude},
            {"role": "assistant", "content": "已读取本对话的长期记忆和早期摘要。"},
        ])
    packed.extend(recent_messages)

    budget = max(CONTEXT_TOKEN_BUDGET - CONTEXT_RESERVED_OUTPUT_TOKENS, 8000)
    packed, truncated_count = _trim_to_budget(packed, budget, current_user_message)
    approx_tokens = _messages_tokens(packed) + estimate_tokens(current_user_message)

    result = ContextBuildResult(
        history=packed,
        approx_input_tokens=approx_tokens,
        raw_message_count=len(raw_history),
        sent_message_count=len(packed),
        summarized_message_count=len(older_messages),
        truncated_message_count=truncated_count,
        context_token_budget=CONTEXT_TOKEN_BUDGET,
    )
    _upsert_stats(db, user_id, conversation_id, result)
    return result


def _normalize_message(message: dict[str, Any]) -> dict[str, Any] | None:
    role = (message or {}).get("role")
    if role not in {"user", "assistant"}:
        return None
    return {"role": role, "content": (message or {}).get("content", "")}


def _content_part_to_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    if part.get("type") == "text":
        return str(part.get("text", ""))
    if part.get("kind") == "document":
        return str(part.get("extracted_text", ""))
    return str(part.get("text") or part.get("content") or "")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(_content_part_to_text(part) for part in content).strip()
    return str(content or "").strip()


def _messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(m.get("content", "")) + 4 for m in messages)


def _build_extractive_summary(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, message in enumerate(messages, start=1):
        text = _message_text(message)
        if not text:
            continue
        text = " ".join(text.split())
        if len(text) > 260:
            text = text[:260].rstrip() + "..."
        speaker = "用户" if message["role"] == "user" else "助手"
        lines.append(f"{idx}. {speaker}: {text}")
    summary = "\n".join(lines)
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[-SUMMARY_MAX_CHARS:]
        first_break = summary.find("\n")
        if first_break >= 0:
            summary = summary[first_break + 1 :]
    return summary


def _build_prelude(summary_text: str, memories: list[str]) -> str:
    parts: list[str] = []
    if memories:
        parts.append("【本对话关键记忆】\n" + "\n".join(f"- {m}" for m in memories[:20]))
    if summary_text:
        parts.append("【本对话早期摘要】\n" + summary_text)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n请把以上内容当作当前对话的上下文，不要向用户复述这段摘要。"


def _trim_to_budget(
    messages: list[dict[str, Any]],
    budget: int,
    current_user_message: str,
) -> tuple[list[dict[str, Any]], int]:
    trimmed = list(messages)
    truncated = 0
    while trimmed and _messages_tokens(trimmed) + estimate_tokens(current_user_message) > budget:
        # 优先保留最前面的摘要/记忆 prelude，因此从真实最近消息的开头按轮次裁。
        remove_idx = 2 if len(trimmed) > 2 and _message_text(trimmed[0]).startswith("【") else 0
        if remove_idx >= len(trimmed):
            remove_idx = 0
        trimmed.pop(remove_idx)
        truncated += 1
        if remove_idx < len(trimmed):
            trimmed.pop(remove_idx)
            truncated += 1
    return trimmed, truncated


def _load_summary(db: Session, user_id: int, conversation_id: int | None) -> str:
    if not conversation_id:
        return ""
    row = db.query(ConversationSummary).filter(
        ConversationSummary.user_id == user_id,
        ConversationSummary.conversation_id == conversation_id,
    ).first()
    return row.summary if row else ""


def _load_memories(db: Session, user_id: int, conversation_id: int | None) -> list[str]:
    if not conversation_id:
        return []
    rows = db.query(ConversationMemory).filter(
        ConversationMemory.user_id == user_id,
        ConversationMemory.conversation_id == conversation_id,
        ConversationMemory.is_active == True,  # noqa: E712
    ).order_by(ConversationMemory.updated_at.desc(), ConversationMemory.id.desc()).limit(20).all()
    return [r.content for r in rows if r.content]


def _upsert_summary(
    db: Session,
    user_id: int,
    conversation_id: int | None,
    summary: str,
    summarized_message_count: int,
) -> None:
    if not conversation_id or not summary:
        return
    row = db.query(ConversationSummary).filter(
        ConversationSummary.user_id == user_id,
        ConversationSummary.conversation_id == conversation_id,
    ).first()
    if not row:
        row = ConversationSummary(user_id=user_id, conversation_id=conversation_id)
        db.add(row)
    row.summary = summary
    row.summarized_message_count = summarized_message_count
    row.approx_tokens = estimate_tokens(summary)
    row.updated_at = datetime.utcnow()
    db.flush()


def _upsert_stats(
    db: Session,
    user_id: int,
    conversation_id: int | None,
    result: ContextBuildResult,
) -> None:
    if not conversation_id:
        return
    row = db.query(ConversationContextStat).filter(
        ConversationContextStat.user_id == user_id,
        ConversationContextStat.conversation_id == conversation_id,
    ).first()
    if not row:
        row = ConversationContextStat(user_id=user_id, conversation_id=conversation_id)
        db.add(row)
    row.context_token_budget = result.context_token_budget
    row.approx_input_tokens = result.approx_input_tokens
    row.raw_message_count = result.raw_message_count
    row.sent_message_count = result.sent_message_count
    row.summarized_message_count = result.summarized_message_count
    row.truncated_message_count = result.truncated_message_count
    row.updated_at = datetime.utcnow()
    db.flush()
