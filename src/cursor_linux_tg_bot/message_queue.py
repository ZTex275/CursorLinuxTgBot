from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import Update

logger = logging.getLogger(__name__)

ProcessFn = Callable[[Update, str], Awaitable[None]]


@dataclass
class QueuedMessage:
    update: Update
    user_text: str


class MessageQueue:
    """Per-chat FIFO queue: messages are processed one at a time."""

    def __init__(self, *, max_size: int = 100) -> None:
        self._max_size = max_size
        self._queues: dict[int, asyncio.Queue[QueuedMessage]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._handler: ProcessFn | None = None

    def set_handler(self, handler: ProcessFn) -> None:
        self._handler = handler

    def size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

    def _get_queue(self, chat_id: int) -> asyncio.Queue[QueuedMessage]:
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue(maxsize=self._max_size)
        return self._queues[chat_id]

    def _ensure_worker(self, chat_id: int) -> None:
        worker = self._workers.get(chat_id)
        if worker is not None and not worker.done():
            return
        self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    async def enqueue(
        self,
        chat_id: int,
        update: Update,
        user_text: str,
        *,
        running: bool = False,
    ) -> str | None:
        """Add message to queue. Returns user-facing status or None if starts next."""
        queue = self._get_queue(chat_id)
        if queue.full():
            return f"Очередь переполнена (макс. {self._max_size}). Дождитесь выполнения."

        await queue.put(QueuedMessage(update=update, user_text=user_text))
        self._ensure_worker(chat_id)

        ahead = queue.qsize() - 1 + (1 if running else 0)
        if ahead <= 0:
            return None

        if ahead == 1:
            word = "задача"
        elif 2 <= ahead <= 4:
            word = "задачи"
        else:
            word = "задач"
        return f"📋 В очереди: впереди {ahead} {word}"

    async def _worker(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        assert self._handler is not None

        while True:
            item = await queue.get()
            try:
                await self._handler(item.update, item.user_text)
            except Exception:
                logger.exception("queued message failed chat_id=%s", chat_id)
                if item.update.message:
                    try:
                        await item.update.message.reply_text("❌ Ошибка при обработке сообщения из очереди.")
                    except Exception:
                        logger.exception("failed to notify user about queue error")
            finally:
                queue.task_done()

            if queue.empty():
                break

    async def shutdown(self) -> None:
        for task in list(self._workers.values()):
            if task and not task.done():
                task.cancel()
        self._workers.clear()
