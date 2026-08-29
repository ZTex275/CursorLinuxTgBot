from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from cursor_sdk.types import AgentMessage

from .config import CursorConfig
from .session_store import ChatSession

logger = logging.getLogger(__name__)

_COMPACT_ERROR_MARKERS = (
    "authentication error",
    "context",
    "token",
    "too long",
    "length",
    "overflow",
    "limit exceeded",
)

_STALE_SESSION_ERROR_MARKERS = (
    "unknown agent",
    "not found",
    "not_found",
    "active run",
    "agent_not_found",
    "no such agent",
    "expired",
    "stale",
    "unavailable",
    "internal",
)


def needs_compaction(session: ChatSession, cfg: CursorConfig) -> bool:
    if not cfg.auto_compact:
        return False
    if session.turn_count >= cfg.max_turns_before_compact:
        return True
    if session.last_input_tokens >= cfg.max_input_tokens_before_compact:
        return True
    return False


def is_stale_session_run_error(error_text: str | None) -> bool:
    if not error_text:
        return True
    lowered = error_text.lower()
    return any(marker in lowered for marker in _STALE_SESSION_ERROR_MARKERS)


def is_compactable_run_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    lowered = error_text.lower()
    if is_stale_session_run_error(error_text):
        return False
    return any(marker in lowered for marker in _COMPACT_ERROR_MARKERS)


def wrap_prompt_with_summary(summary: str | None, prompt: str) -> str:
    if not summary:
        return prompt
    return (
        "Краткое содержание предыдущего диалога (контекст сжат автоматически):\n\n"
        f"{summary}\n\n"
        "---\n"
        f"{prompt}"
    )


def merge_summaries(*parts: str | None, max_length: int = 8000) -> str:
    combined = "\n\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if len(combined) <= max_length:
        return combined
    return combined[: max_length - 3].rstrip() + "..."


def _text_from_message_payload(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, Mapping):
        return str(raw).strip()

    content = raw.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")).strip())
                elif "text" in block:
                    parts.append(str(block["text"]).strip())
        return "\n".join(part for part in parts if part)
    if "text" in raw:
        return str(raw["text"]).strip()
    return ""


def summary_from_agent_messages(messages: list[AgentMessage], *, max_chars: int = 6000) -> str:
    user_lines: list[str] = []
    assistant_lines: list[str] = []

    for msg in messages:
        text = _text_from_message_payload(msg.message)
        if not text:
            continue
        if len(text) > 1200:
            text = text[:1197].rstrip() + "..."
        if msg.type == "user":
            user_lines.append(text)
        elif msg.type == "assistant":
            assistant_lines.append(text)

    chunks: list[str] = []
    for label, lines in (("Пользователь", user_lines[-4:]), ("Агент", assistant_lines[-4:])):
        if not lines:
            continue
        body = "\n".join(f"- {line}" for line in lines)
        chunks.append(f"{label}:\n{body}")

    summary = "\n\n".join(chunks).strip()
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 3].rstrip() + "..."
