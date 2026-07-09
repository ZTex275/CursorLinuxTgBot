"""VK-бот (Bots Long Poll API) поверх того же Cursor-моста.

Реализован на httpx без сторонних VK-библиотек: сервер может не иметь
доступа к PyPI. Токен — ключ доступа сообщества с правами на сообщения,
group_id — числовой id сообщества.
"""
from __future__ import annotations

import asyncio
import logging
import random
import textwrap

import httpx

from .config import AppConfig
from .cursor_runner import CursorSessionManager, RunUpdate
from .git_manager import GitManager
from .message_queue import MessageQueue
from .textutil import split_message

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.199"


class VkApiError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class VkCursorBot:
    def __init__(
        self,
        config: AppConfig,
        sessions: CursorSessionManager,
        git: GitManager,
    ) -> None:
        self._config = config
        self._vk = config.vk
        self._sessions = sessions
        self._git = git
        self._queue = MessageQueue(max_size=config.bot.max_queue_size)
        self._queue.set_handler(self._process_user_message, on_error=self._notify_queue_error)
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _chat_key(peer_id: int) -> str:
        # Отдельное пространство ключей, чтобы не пересекаться с telegram chat_id
        return f"vk{peer_id}"

    # --- VK API ---

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self._vk.connect_timeout,
                read=self._vk.read_timeout + 30,  # long poll держит соединение открытым
                write=self._vk.read_timeout,
                pool=self._vk.connect_timeout,
            )
            self._client = httpx.AsyncClient(timeout=timeout, proxy=self._vk.proxy or None)
        return self._client

    async def _api(self, method: str, **params) -> dict | int:
        params.setdefault("v", VK_API_VERSION)
        params.setdefault("access_token", self._vk.token)
        response = await self._http().post(f"{VK_API}/{method}", data=params)
        data = response.json()
        if "error" in data:
            err = data["error"]
            raise VkApiError(int(err.get("error_code", 0)), err.get("error_msg", "unknown VK error"))
        return data["response"]

    async def _send(self, peer_id: int, text: str) -> int | None:
        message_id: int | None = None
        for chunk in split_message(text, self._config.bot.max_reply_length):
            result = await self._api(
                "messages.send",
                peer_id=peer_id,
                message=chunk,
                random_id=random.randint(1, 2**31 - 1),
            )
            if isinstance(result, int):
                message_id = result
        return message_id

    async def _edit(self, peer_id: int, message_id: int, text: str) -> bool:
        try:
            await self._api(
                "messages.edit",
                peer_id=peer_id,
                message_id=message_id,
                message=text,
            )
            return True
        except (VkApiError, httpx.HTTPError):
            return False

    async def _delete(self, peer_id: int, message_id: int) -> None:
        try:
            await self._api(
                "messages.delete",
                peer_id=peer_id,
                message_ids=message_id,
                delete_for_all=1,
            )
        except (VkApiError, httpx.HTTPError):
            pass

    # --- авторизация и команды ---

    def _authorized(self, from_id: int) -> bool:
        allowed = self._vk.allowed_user_ids
        if not allowed:
            return True
        return from_id in allowed

    async def _handle_command(self, peer_id: int, text: str) -> bool:
        """Обработка /команд. Возвращает True, если сообщение было командой."""
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]
        chat_key = self._chat_key(peer_id)

        if command in {"/start", "начать"}:
            await self._send(peer_id, self._config.bot.welcome_message)
        elif command == "/new":
            await self._sessions.reset_chat(chat_key)
            await self._send(peer_id, "Новая сессия Cursor. История и git-чекпоинт сброшены.")
        elif command == "/mode":
            if not args or args[0] not in {"agent", "plan"}:
                await self._send(peer_id, "Использование: /mode agent | plan")
            else:
                self._config.cursor.mode = args[0]
                await self._send(peer_id, f"Режим по умолчанию: {args[0]}")
        elif command == "/status":
            cursor = self._config.cursor
            git_on = self._config.git.enabled and await self._git.is_repo()
            await self._send(
                peer_id,
                textwrap.dedent(
                    f"""
                    workspace: {cursor.workspace}
                    model: {cursor.model}
                    mode: {cursor.mode}
                    git: {"включён" if git_on else "выкл / не репозиторий"}
                    auto_commit: {self._config.git.auto_commit}
                    """
                ).strip(),
            )
        elif command == "/queue":
            pending = self._queue.size(chat_key)
            if pending == 0:
                await self._send(peer_id, "Очередь пуста.")
            else:
                word = "сообщение" if pending == 1 else "сообщения" if 2 <= pending <= 4 else "сообщений"
                await self._send(peer_id, f"В очереди: {pending} {word}.")
        elif command == "/commit":
            await self._cmd_commit(peer_id, args)
        elif command == "/undo":
            await self._cmd_undo(peer_id)
        else:
            return False
        return True

    async def _cmd_commit(self, peer_id: int, args: list[str]) -> None:
        if not self._config.git.enabled:
            await self._send(peer_id, "Git отключён в config.yaml")
            return
        if not await self._git.is_repo():
            await self._send(peer_id, "workspace не является git-репозиторием")
            return

        chat_key = self._chat_key(peer_id)
        if args:
            message = " ".join(args)
        else:
            session = self._sessions.load_session(chat_key)
            message = session.last_user_message or ""
            if not message:
                await self._send(peer_id, "Использование: /commit <сообщение>")
                return
            message = GitManager.format_commit_message(
                message,
                prefix=self._config.git.commit_prefix,
                max_length=self._config.git.max_commit_message_length,
            )

        result = await self._git.commit(message)
        if result.ok:
            self._sessions.clear_git_checkpoint(chat_key)
            await self._send(peer_id, f"✅ {result.message}")
        else:
            await self._send(peer_id, f"ℹ️ {result.message}")

    async def _cmd_undo(self, peer_id: int) -> None:
        if not self._config.git.enabled:
            await self._send(peer_id, "Git отключён в config.yaml")
            return
        if not await self._git.is_repo():
            await self._send(peer_id, "workspace не является git-репозиторием")
            return

        chat_key = self._chat_key(peer_id)
        session = self._sessions.load_session(chat_key)
        if not session.git_checkpoint:
            await self._send(peer_id, "Нет точки отката для последнего сообщения.")
            return

        lock = self._sessions.lock_for(chat_key)
        if lock.locked():
            await self._send(peer_id, "Подождите — сейчас выполняется задача из очереди.")
            return

        async with lock:
            try:
                msg = await self._git.rollback(session.git_checkpoint)
            except Exception as err:
                logger.exception("git rollback failed (vk)")
                await self._send(peer_id, f"❌ Откат не удался: {err}")
                return
            self._sessions.clear_git_checkpoint(chat_key)
            await self._send(peer_id, f"✅ {msg}")

    # --- обработка сообщений ---

    async def _notify_queue_error(self, peer_id: int) -> None:
        await self._send(peer_id, "❌ Ошибка при обработке сообщения из очереди.")

    async def _maybe_auto_commit(self, user_text: str) -> str | None:
        if not self._config.git.enabled or not self._config.git.auto_commit:
            return None
        if not await self._git.is_repo():
            return None
        if not await self._git.has_changes():
            return None

        message = GitManager.format_commit_message(
            user_text,
            prefix=self._config.git.commit_prefix,
            max_length=self._config.git.max_commit_message_length,
        )
        result = await self._git.commit(message)
        if result.ok:
            return result.message
        logger.warning("auto-commit failed (vk): %s", result.message)
        return None

    async def _stream_reply(self, peer_id: int, stream) -> RunUpdate | None:
        status_id = await self._send(peer_id, "⏳ Cursor думает…")
        last_edit = 0.0
        interval = self._config.bot.stream_edit_interval_sec
        limit = self._config.bot.max_reply_length
        final_item: RunUpdate | None = None

        async for item in stream:
            final_item = item
            if item.error:
                if status_id is None or not await self._edit(peer_id, status_id, f"❌ {item.error}"):
                    await self._send(peer_id, f"❌ {item.error}")
                return final_item

            now = asyncio.get_event_loop().time()
            if not item.done and now - last_edit < interval:
                continue
            last_edit = now

            if item.done:
                if status_id is not None:
                    await self._delete(peer_id, status_id)
                await self._send(peer_id, item.text or "Готово.")
            elif status_id is not None:
                preview = item.text.strip() or "…"
                if len(preview) > limit:
                    preview = preview[: limit - 1] + "…"
                await self._edit(peer_id, status_id, preview)

        return final_item

    async def _process_user_message(self, peer_id: int, user_text: str) -> None:
        chat_key = self._chat_key(peer_id)
        prompt = f"{self._config.bot.system_prefix}\n\n{user_text}"

        lock = self._sessions.lock_for(chat_key)
        async with lock:
            if self._config.git.enabled:
                if not await self._git.is_repo():
                    await self._send(
                        peer_id,
                        "⚠️ workspace не git-репозиторий — /undo и авто-коммит недоступны.",
                    )
                else:
                    try:
                        checkpoint = await self._git.create_checkpoint(chat_key)
                        self._sessions.set_git_checkpoint(chat_key, checkpoint, user_text)
                    except Exception as err:
                        logger.exception("git checkpoint failed (vk)")
                        await self._send(peer_id, f"❌ Git checkpoint: {err}")
                        return

            stream = self._sessions.run_prompt(chat_key, prompt, mode=self._config.cursor.mode)
            final = await self._stream_reply(peer_id, stream)

            if final and not final.error and self._config.git.enabled:
                commit_note = await self._maybe_auto_commit(user_text)
                if commit_note:
                    await self._send(peer_id, f"📌 {commit_note}")

    async def _on_message(self, message: dict) -> None:
        peer_id = int(message.get("peer_id", 0))
        from_id = int(message.get("from_id", 0))
        text = (message.get("text") or "").strip()
        if not peer_id or not text:
            return

        if not self._authorized(from_id):
            await self._send(peer_id, "Доступ запрещён. Добавьте свой VK user id в config.yaml (vk.allowed_user_ids).")
            return

        if text.startswith("/") or text.lower() == "начать":
            if await self._handle_command(peer_id, text):
                return

        chat_key = self._chat_key(peer_id)
        lock = self._sessions.lock_for(chat_key)
        status = await self._queue.enqueue(chat_key, peer_id, text, running=lock.locked())
        if status:
            await self._send(peer_id, status)

    # --- long poll ---

    async def _get_long_poll_server(self) -> tuple[str, str, str]:
        response = await self._api("groups.getLongPollServer", group_id=self._vk.group_id)
        assert isinstance(response, dict)
        return response["server"], response["key"], response["ts"]

    async def poll_loop(self) -> None:
        logger.info("VK bot: long poll started (group %s)", self._vk.group_id)
        server = key = ts = None

        while True:
            try:
                if server is None:
                    server, key, ts = await self._get_long_poll_server()

                response = await self._http().get(
                    server,
                    params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                )
                data = response.json()

                if "failed" in data:
                    fail = data["failed"]
                    if fail == 1:
                        ts = data.get("ts", ts)
                    else:
                        server = None  # ключ устарел — переполучить сервер
                    continue

                ts = data.get("ts", ts)
                for update in data.get("updates", []):
                    if update.get("type") != "message_new":
                        continue
                    message = (update.get("object") or {}).get("message") or {}
                    try:
                        await self._on_message(message)
                    except Exception:
                        logger.exception("VK message handling failed")

            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, VkApiError, ValueError) as err:
                logger.warning("VK long poll error: %s — retry in 10s", err)
                server = None
                await asyncio.sleep(10)

    async def run_standalone(self) -> None:
        """Запуск без Telegram: сам стартует и останавливает Cursor-мост."""
        await self._sessions.start()
        logger.info("Cursor bridge started for workspace %s", self._config.cursor.workspace)
        try:
            await self.poll_loop()
        finally:
            await self._queue.shutdown()
            await self._sessions.stop()
            await self.aclose()

    async def aclose(self) -> None:
        await self._queue.shutdown()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
