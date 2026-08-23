from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from .agent_base import RunUpdate
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
    started_at: float | None = None,
    initial_stage: str | None = "Запуск агента",
) -> RunUpdate | None:
    """Во время работы — статус с этапом и таймером; финал — полный ответ с «✅ Готово»."""
    task_started_at = started_at if started_at is not None else time.monotonic()
    progress = {"stage": initial_stage}
    final_item: RunUpdate | None = None
    stop_ticker = asyncio.Event()

    async def ticker() -> None:
        while not stop_ticker.is_set():
            try:
                await asyncio.wait_for(stop_ticker.wait(), timeout=stream_edit_interval_sec)
                break
            except TimeoutError:
                pass
            try:
                await edit_status(
                    status_message_id,
                    working_status(provider_label, task_started_at, progress["stage"]),
                )
            except Exception:
                pass

    ticker_task = asyncio.create_task(ticker())
    try:
        async for item in stream:
            final_item = item
            if item.stage:
                progress["stage"] = item.stage

            if item.cancelled:
                await edit_status(status_message_id, "⛔ Остановлено пользователем.")
                return final_item

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

        if final_item is None:
            await edit_status(status_message_id, "❌ Нет ответа от агента.\n\n⛔ Остановлено")
        return final_item
    finally:
        stop_ticker.set()
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
