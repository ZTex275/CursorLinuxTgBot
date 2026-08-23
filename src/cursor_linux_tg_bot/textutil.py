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


_TOOL_STAGE_LABELS: dict[str, str] = {
    "Shell": "Выполняю команду в терминале",
    "Read": "Читаю файл",
    "Grep": "Ищу по файлам",
    "Write": "Записываю файл",
    "StrReplace": "Редактирую файл",
    "Delete": "Удаляю файл",
    "Glob": "Ищу файлы",
    "Task": "Запускаю подзадачу",
    "WebSearch": "Ищу в интернете",
    "WebFetch": "Загружаю страницу",
    "SwitchMode": "Переключаю режим",
    "ReadLints": "Проверяю ошибки линтера",
    "EditNotebook": "Редактирую notebook",
    "Await": "Ожидаю завершения команды",
    "TodoWrite": "Обновляю список задач",
}


def stage_from_tool_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return "Выполняю инструмент"
    return _TOOL_STAGE_LABELS.get(clean, f"Инструмент: {clean}")


def stage_from_sdk_message(message: object) -> str | None:
    msg_type = getattr(message, "type", None)

    if msg_type == "thinking":
        return "Думаю над задачей"

    if msg_type == "tool_call":
        name = getattr(message, "name", "") or ""
        status = str(getattr(message, "status", "") or "")
        label = stage_from_tool_name(name)
        if status == "running":
            return label
        if status == "completed":
            return f"{label} — готово"
        if status == "error":
            return f"{label} — ошибка"
        return label

    if msg_type == "status":
        text = (getattr(message, "message", "") or getattr(message, "status", "") or "").strip()
        return f"Статус: {text}" if text else None

    if msg_type == "task":
        text = (getattr(message, "text", "") or getattr(message, "status", "") or "").strip()
        return text or None

    if msg_type == "assistant":
        return "Формирую ответ"

    return None


def working_status(label: str, started_at: float, stage: str | None = None) -> str:
    elapsed = max(0, int(time.monotonic() - started_at))
    lines = [f"⏳ {label}"]
    if stage:
        lines.append(f"Этап: {stage}")
    else:
        lines.append("Этап: выполняется задача")
    lines.append(f"Ожидание: {elapsed} с")
    return "\n".join(lines)


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
