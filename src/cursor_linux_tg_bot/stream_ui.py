from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from .cursor_runner import RunUpdate
from .textutil import format_final_reply, split_message, working_status

SendTextFn = Callable[[str], Awaitable[None]]
EditTextFn = Callable[[str, str], Awaitable[None]]
DeleteFn = Callable[[str], Awaitable[None]]


async def deliver_streamed_reply(
    stream: AsyncIterator[RunUpdate],
    *,
    provider_label: str,
    max_reply_length: int,
    stream_edit_interval_sec: float,
    send_text: SendTextFn,
    edit_status: EditTextFn,
    delete_status: DeleteFn,
    status_message_id: str,
) -> RunUpdate | None:
    """Во время работы — только статус с таймером; финал — полный ответ с «✅ Готово»."""
    started_at = time.monotonic()
    last_edit = 0.0
    final_item: RunUpdate | None = None

    async for item in stream:
        final_item = item
        if item.error:
            await edit_status(status_message_id, f"❌ {item.error}\n\n⛔ Остановлено")
            return final_item

        if item.done:
            try:
                await delete_status(status_message_id)
            except Exception:
                pass
            for chunk in split_message(format_final_reply(item.text), max_reply_length):
                await send_text(chunk)
            return final_item

        now = time.monotonic()
        if now - last_edit < stream_edit_interval_sec:
            continue
        last_edit = now
        try:
            await edit_status(status_message_id, working_status(provider_label, started_at))
        except Exception:
            pass

    if final_item is None:
        await edit_status(status_message_id, "❌ Нет ответа от агента.\n\n⛔ Остановлено")
    return final_item
