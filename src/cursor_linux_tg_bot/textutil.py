from __future__ import annotations

import re
import time
import traceback

_MD_CODE_BLOCK_RE = re.compile(r"```(?:[^\n]*\n)?(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_ORPHAN_RE = re.compile(r"\*\*|__|`")


def plain_text(text: str) -> str:
    """Убирает Markdown-разметку для чатов без parse_mode."""
    if not text:
        return text

    result = text
    result = _MD_CODE_BLOCK_RE.sub(lambda match: match.group(1).strip(), result)
    result = _MD_INLINE_CODE_RE.sub(r"\1", result)
    result = _MD_IMAGE_RE.sub(r"\1", result)
    result = _MD_LINK_RE.sub(r"\1", result)
    result = _MD_STRIKE_RE.sub(r"\1", result)
    result = _MD_BOLD_RE.sub(lambda match: match.group(1) or match.group(2), result)
    result = _MD_ITALIC_RE.sub(lambda match: match.group(1) or match.group(2), result)
    result = _MD_HEADER_RE.sub("", result)
    result = _MD_ORPHAN_RE.sub("", result)
    return result


def split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n ")
    return chunks


def working_status(label: str, started_at: float) -> str:
    elapsed = max(0, int(time.monotonic() - started_at))
    phase = elapsed // 2
    dots = "." * (1 + (phase % 3))
    return f"⏳ {label} выполняет задачу{dots}\nПожалуйста, подождите ({elapsed} с)"


def format_final_reply(text: str) -> str:
    body = plain_text((text or "").strip())
    if not body:
        body = "Агент завершил работу без текстового ответа."
    return f"{body}\n\n✅ Готово"


def format_queue_error(err: BaseException, *, max_length: int = 4000) -> str:
    header = "❌ Ошибка при обработке сообщения из очереди."
    err_msg = str(err).strip() or "(без текста)"

    parts = [
        header,
        "",
        f"Тип: {type(err).__name__}",
        f"Причина: {err_msg}",
    ]

    tb_lines = traceback.format_exception(type(err), err, err.__traceback__)
    tb_text = "".join(tb_lines).strip()
    if tb_text:
        lines = tb_text.splitlines()
        max_tb_lines = 12
        if len(lines) > max_tb_lines:
            lines = ["…"] + lines[-(max_tb_lines - 1) :]
        parts.extend(["", "Трассировка:", *lines])

    text = "\n".join(parts)
    if len(text) > max_length:
        return text[: max_length - 1] + "…"
    return text
