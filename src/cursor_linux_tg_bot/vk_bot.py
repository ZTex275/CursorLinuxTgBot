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
import time

import httpx

from .agent_base import RunUpdate
from .agent_factory import create_session_manager
from .config import AppConfig, provider_status_text
from .git_helpers import append_push_note, format_commit_reply
from .git_manager import GitManager
from .message_queue import ChatKey, MessageQueue
from .model_switch import switch_model
from .provider_switch import switch_provider
from .service_reload import BotReloader, augment_prompt
from .stream_ui import deliver_streamed_reply
from .textutil import extract_command_payload, format_queue_error, split_message, working_status

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.199"


class VkApiError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


_VK_SCOPE_HINT = """\
VK: у токена нет прав для Long Poll (ошибка 15).
Создайте НОВЫЙ ключ доступа сообщества:
  Управление → Работа с API → Ключи доступа
  Отметьте: «Сообщения сообщества» и «Управление сообществом»
  НЕ используйте пользовательский OAuth-токен — только ключ сообщества.
Затем: Работа с API → Long Poll API → Включить → событие «Входящее сообщение».
И проверьте vk.group_id в config.yaml (числовой id без минуса)."""


class VkCursorBot:
    def __init__(
        self,
        config: AppConfig,
        sessions,
        git: GitManager,
    ) -> None:
        self._config = config
        self._vk = config.vk
        self._sessions = sessions
        self._git = git
        self._reloader = BotReloader(config.workspace, config.service)
        self._sessions_sync = None
        self._extra_queues: tuple[MessageQueue, ...] = ()
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
            await self._send(peer_id, f"Новая сессия {self._config.provider_label}. История и git-чекпоинт сброшены.")
        elif command == "/mode":
            if not args or args[0] not in {"agent", "plan"}:
                await self._send(peer_id, "Использование: /mode agent | plan")
            else:
                self._config.mode = args[0]
                if self._config.cursor is not None:
                    self._config.cursor.mode = args[0]
                if self._config.openrouter is not None:
                    self._config.openrouter.mode = args[0]
                if self._config.openrouter_cli is not None:
                    self._config.openrouter_cli.mode = args[0]
                await self._send(peer_id, f"Режим по умолчанию: {args[0]}")
        elif command == "/provider":
            new_provider = args[0].strip().lower() if args else None
            new_sessions, message = await switch_provider(
                self._config,
                self._sessions,
                new_provider,
                queues=(self._queue, *self._extra_queues),
            )
            if new_sessions is not None:
                self._sessions = new_sessions
                if self._sessions_sync is not None:
                    self._sessions_sync(new_sessions)
            await self._send(peer_id, message)
        elif command == "/model":
            new_model = " ".join(args).strip() if args else None
            message = await switch_model(
                self._config,
                self._sessions,
                new_model,
                queues=(self._queue, *self._extra_queues),
            )
            await self._send(peer_id, message)
        elif command == "/status":
            git_on = self._config.git.enabled and await self._git.is_repo()
            gh = "токен задан" if self._config.git.github_token else "токен не задан"
            await self._send(
                peer_id,
                textwrap.dedent(
                    f"""
                    {provider_status_text(self._config)}
                    workspace: {self._config.workspace}
                    model: {self._config.model}
                    mode: {self._config.mode}
                    git: {"включён" if git_on else "выкл / не репозиторий"}
                    auto_commit: {self._config.git.auto_commit}
                    auto_push: {self._config.git.auto_push}
                    github: {gh}
                    """
                ).strip(),
            )
        elif command == "/git":
            if not self._config.git.enabled:
                await self._send(peer_id, "Git отключён в config.yaml")
            else:
                await self._send(peer_id, await self._git.status_summary())
        elif command == "/push":
            await self._cmd_push(peer_id)
        elif command == "/pull":
            await self._cmd_pull(peer_id)
        elif command == "/queue":
            pending = self._queue.size(chat_key)
            if pending == 0:
                await self._send(peer_id, "Очередь пуста.")
            else:
                word = "сообщение" if pending == 1 else "сообщения" if 2 <= pending <= 4 else "сообщений"
                await self._send(peer_id, f"В очереди: {pending} {word}.")
        elif command == "/commit":
            await self._cmd_commit(peer_id, text)
        elif command == "/undo":
            await self._cmd_undo(peer_id)
        elif command == "/stop":
            await self._cmd_stop(peer_id)
        else:
            return False
        return True

    async def _cmd_commit(self, peer_id: int, text: str) -> None:
        if not self._config.git.enabled:
            await self._send(peer_id, "Git отключён в config.yaml")
            return
        if not await self._git.is_repo():
            await self._send(peer_id, "workspace не является git-репозиторием")
            return

        chat_key = self._chat_key(peer_id)
        payload = extract_command_payload(text, "commit")
        if payload:
            message = payload
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
        await self._send(
            peer_id,
            await format_commit_reply(self._git, result, self._config.git),
        )

    async def _cmd_push(self, peer_id: int) -> None:
        if not self._config.git.enabled:
            await self._send(peer_id, "Git отключён в config.yaml")
            return
        if not self._config.git.github_token:
            await self._send(peer_id, "Задайте git.github_token: ${GITHUB_TOKEN} в config.yaml и .env")
            return
        result = await self._git.push()
        prefix = "✅" if result.ok else "❌"
        await self._send(peer_id, f"{prefix} {result.message}")

    async def _cmd_pull(self, peer_id: int) -> None:
        if not self._config.git.enabled:
            await self._send(peer_id, "Git отключён в config.yaml")
            return
        if not self._config.git.github_token:
            await self._send(peer_id, "Задайте git.github_token: ${GITHUB_TOKEN} в config.yaml и .env")
            return
        result = await self._git.pull()
        prefix = "✅" if result.ok else "❌"
        await self._send(peer_id, f"{prefix} {result.message}")

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

    async def _stop_chat(self, chat_key: ChatKey) -> str:
        lock = self._sessions.lock_for(chat_key)
        running = lock.locked()

        canceled = False
        if running:
            canceled = await self._sessions.cancel_active(chat_key)

        cleared = self._queue.clear(chat_key)

        rolled_back = False
        if running and self._config.git.enabled and await self._git.is_repo():
            session = self._sessions.load_session(chat_key)
            checkpoint = session.git_checkpoint
            if checkpoint:
                try:
                    async with lock:
                        await self._git.rollback(checkpoint)
                        self._sessions.clear_git_checkpoint(chat_key)
                    rolled_back = True
                except Exception as err:
                    logger.exception("git rollback on stop failed (vk)")
                    return f"⚠️ Остановлено, но откат не удался: {err}"

        parts: list[str] = []
        if canceled:
            parts.append("текущая задача прервана")
        if cleared:
            word = "сообщение" if cleared == 1 else "сообщения" if 2 <= cleared <= 4 else "сообщений"
            parts.append(f"из очереди удалено {cleared} {word}")
        if rolled_back:
            parts.append("изменения откачены")
        if not parts:
            return "Нечего останавливать — нет активной задачи и очередь пуста."
        return "✅ " + "; ".join(parts) + "."

    async def _cmd_stop(self, peer_id: int) -> None:
        chat_key = self._chat_key(peer_id)
        await self._send(peer_id, await self._stop_chat(chat_key))

    # --- обработка сообщений ---

    async def _notify_queue_error(self, peer_id: int, err: BaseException) -> None:
        text = format_queue_error(err, max_length=self._config.bot.max_reply_length)
        await self._send(peer_id, text)

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
            return await append_push_note(self._git, result.message, self._config.git)
        logger.warning("auto-commit failed (vk): %s", result.message)
        return None

    async def _stream_reply(
        self,
        peer_id: int,
        stream,
        *,
        status_id: int | None = None,
        started_at: float | None = None,
        initial_stage: str | None = "Запуск агента",
    ) -> RunUpdate | None:
        task_started_at = started_at if started_at is not None else time.monotonic()
        if status_id is None:
            status_id = await self._send(
                peer_id,
                working_status(self._config.provider_label, task_started_at, initial_stage),
            )
        if status_id is None:
            status_id = 0

        async def send_text(text: str) -> None:
            await self._send(peer_id, text)

        async def edit_status(_msg_id: str, text: str) -> None:
            if status_id:
                await self._edit(peer_id, status_id, text)

        async def delete_status(_msg_id: str) -> None:
            if status_id:
                await self._delete(peer_id, status_id)

        return await deliver_streamed_reply(
            stream,
            provider_label=self._config.provider_label,
            max_reply_length=self._config.bot.max_reply_length,
            stream_edit_interval_sec=self._config.bot.stream_edit_interval_sec,
            send_text=send_text,
            edit_status=edit_status,
            delete_status=delete_status,
            status_message_id=str(status_id),
            started_at=task_started_at,
            initial_stage=initial_stage,
        )

    async def _process_user_message(self, peer_id: int, user_text: str) -> None:
        chat_key = self._chat_key(peer_id)
        started_at = time.monotonic()
        status_id = await self._send(
            peer_id,
            working_status(self._config.provider_label, started_at, "Принято сообщение"),
        )

        async def set_stage(stage: str) -> None:
            if not status_id:
                return
            try:
                await self._edit(
                    peer_id,
                    status_id,
                    working_status(self._config.provider_label, started_at, stage),
                )
            except Exception:
                pass

        prompt = augment_prompt(
            self._config.bot.system_prefix,
            user_text,
            include_reload_hint=self._reloader.enabled(),
        )
        start_sha: str | None = None

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
                        await set_stage("Создаю git-чекпоинт")
                        checkpoint = await self._git.create_checkpoint(chat_key)
                        self._sessions.set_git_checkpoint(chat_key, checkpoint, user_text)
                        if self._reloader.enabled():
                            start_sha = checkpoint.head_sha if checkpoint else await self._reloader.snapshot_sha(self._git)
                    except Exception as err:
                        logger.exception("git checkpoint failed (vk)")
                        if status_id:
                            await self._edit(peer_id, status_id, f"❌ Git checkpoint: {err}")
                        return
            elif self._reloader.enabled():
                start_sha = await self._reloader.snapshot_sha(self._git)

            await set_stage("Запуск агента")
            stream = self._sessions.run_prompt(chat_key, prompt, mode=self._config.mode)
            final = await self._stream_reply(
                peer_id,
                stream,
                status_id=status_id,
                started_at=started_at,
                initial_stage="Запуск агента",
            )

            if final and not final.error and not final.cancelled and self._config.git.enabled:
                commit_note = await self._maybe_auto_commit(user_text)
                if commit_note:
                    await self._send(peer_id, f"📌 {commit_note}")

            await self._reloader.maybe_restart_after_task(
                git=self._git,
                start_sha=start_sha,
                final=final,
                notify=lambda text: self._send(peer_id, text),
            )

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
            except VkApiError as err:
                if err.code in {5, 15, 27}:
                    logger.error("%s", _VK_SCOPE_HINT)
                    await asyncio.sleep(60)
                else:
                    logger.warning("VK long poll error: %s — retry in 10s", err)
                    await asyncio.sleep(10)
                server = None
            except (httpx.HTTPError, ValueError) as err:
                logger.warning("VK long poll error: %s — retry in 10s", err)
                server = None
                await asyncio.sleep(10)

    async def run_standalone(self) -> None:
        """Запуск без Telegram: сам стартует и останавливает Cursor-мост."""
        await self._sessions.start()
        logger.info("%s started for workspace %s", self._config.provider_label, self._config.workspace)
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
