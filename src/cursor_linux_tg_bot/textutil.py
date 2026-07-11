from __future__ import annotations

import time


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
    body = (text or "").strip()
    if not body:
        body = "Агент завершил работу без текстового ответа."
    return f"{body}\n\n✅ Готово"
