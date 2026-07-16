from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import textwrap
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .agent_base import RunUpdate
from .agent_factory import create_session_manager
from .config import AppConfig, load_config, provider_status_text
from .git_helpers import append_push_note, format_commit_reply
from .git_manager import GitManager
from .message_queue import ChatKey, MessageQueue
from .network import enable_ipv4_only, telegram_transport
from .provider_switch import switch_provider
from .stream_ui import deliver_streamed_reply
from .textutil import split_message
from .vk_bot import VkCursorBot

logger = logging.getLogger(__name__)


class TelegramCursorBot:
    def __init__(
        self,
        config: AppConfig,
        *,
        sessions=None,
        git: GitManager | None = None,
        vk_bot: VkCursorBot | None = None,
    ) -> None:
        self._config = config
        self._sessions = sessions or create_session_manager(config)
        self._git = git or GitManager.from_config(config.workspace, config.git)
        self._vk_bot = vk_bot
        self._vk_task: asyncio.Task[None] | None = None
        self._queue = MessageQueue(max_size=config.bot.max_queue_size)
        self._queue.set_handler(self._process_user_message, on_error=self._notify_queue_error)

    async def _notify_queue_error(self, update: Update) -> None:
        if update.message:
            await update.message.reply_text("❌ Ошибка при обработке сообщения из очереди.")

    async def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        allowed = self._config.telegram.allowed_user_ids
        if not allowed:
            return True
        return user.id in allowed

    async def _deny(self, update: Update) -> None:
        if update.message:
            await update.message.reply_text("Доступ запрещён. Добавьте свой Telegram user id в config.yaml.")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if update.message:
            await update.message.reply_text(self._config.bot.welcome_message)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        await self._sessions.reset_chat(chat_id)
        if update.message:
            await update.message.reply_text(f"Новая сессия {self._config.provider_label}. История и git-чекпоинт сброшены.")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not context.args or context.args[0] not in {"agent", "plan"}:
            await update.message.reply_text("Использование: /mode agent | plan")
            return
        self._config.mode = context.args[0]
        if self._config.cursor is not None:
            self._config.cursor.mode = context.args[0]
        if self._config.openrouter is not None:
            self._config.openrouter.mode = context.args[0]
        if self._config.openrouter_cli is not None:
            self._config.openrouter_cli.mode = context.args[0]
        await update.message.reply_text(f"Режим по умолчанию: {context.args[0]}")

    async def cmd_provider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return

        queues = (self._queue,)
        if self._vk_bot is not None:
            queues = (self._queue, self._vk_bot._queue)

        new_provider = context.args[0].strip().lower() if context.args else None
        new_sessions, message = await switch_provider(
            self._config,
            self._sessions,
            new_provider,
            queues=queues,
        )
        if new_sessions is not None:
            self._sessions = new_sessions
            if self._vk_bot is not None:
                self._vk_bot._sessions = new_sessions
        await update.message.reply_text(message)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        git_on = self._config.git.enabled and await self._git.is_repo()
        gh = "токен задан" if self._config.git.github_token else "токен не задан"
        text = textwrap.dedent(
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
        ).strip()
        await update.message.reply_text(text)

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not self._config.git.enabled:
            await update.message.reply_text("Git отключён в config.yaml")
            return
        await update.message.reply_text(await self._git.status_summary())

    async def cmd_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not self._config.git.enabled:
            await update.message.reply_text("Git отключён в config.yaml")
            return
        if not self._config.git.github_token:
            await update.message.reply_text("Задайте git.github_token: ${GITHUB_TOKEN} в config.yaml и .env")
            return
        result = await self._git.push()
        prefix = "✅" if result.ok else "❌"
        await update.message.reply_text(f"{prefix} {result.message}")

    async def cmd_pull(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not self._config.git.enabled:
            await update.message.reply_text("Git отключён в config.yaml")
            return
        if not self._config.git.github_token:
            await update.message.reply_text("Задайте git.github_token: ${GITHUB_TOKEN} в config.yaml и .env")
            return
        result = await self._git.pull()
        prefix = "✅" if result.ok else "❌"
        await update.message.reply_text(f"{prefix} {result.message}")

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        pending = self._queue.size(chat_id)
        if pending == 0:
            await update.message.reply_text("Очередь пуста.")
        else:
            word = "сообщение" if pending == 1 else "сообщения" if 2 <= pending <= 4 else "сообщений"
            await update.message.reply_text(f"В очереди: {pending} {word}.")

    async def _stop_chat(self, chat_id: ChatKey) -> str:
        lock = self._sessions.lock_for(chat_id)
        running = lock.locked()

        canceled = False
        if running:
            canceled = await self._sessions.cancel_active(chat_id)

        cleared = self._queue.clear(chat_id)

        rolled_back = False
        if running and self._config.git.enabled and await self._git.is_repo():
            session = self._sessions.load_session(chat_id)
            checkpoint = session.git_checkpoint
            if checkpoint:
                try:
                    async with lock:
                        await self._git.rollback(checkpoint)
                        self._sessions.clear_git_checkpoint(chat_id)
                    rolled_back = True
                except Exception as err:
                    logger.exception("git rollback on stop failed")
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

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        msg = await self._stop_chat(chat_id)
        await update.message.reply_text(msg)

    async def cmd_commit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not self._config.git.enabled:
            await update.message.reply_text("Git отключён в config.yaml")
            return
        if not await self._git.is_repo():
            await update.message.reply_text("workspace не является git-репозиторием")
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        if context.args:
            message = " ".join(context.args)
        else:
            session = self._sessions.load_session(chat_id)
            message = session.last_user_message or ""
            if not message:
                await update.message.reply_text("Использование: /commit <сообщение>")
                return
            message = GitManager.format_commit_message(
                message,
                prefix=self._config.git.commit_prefix,
                max_length=self._config.git.max_commit_message_length,
            )

        result = await self._git.commit(message)
        if result.ok:
            self._sessions.clear_git_checkpoint(chat_id)
        await update.message.reply_text(
            await format_commit_reply(self._git, result, self._config.git)
        )

    async def cmd_undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not self._config.git.enabled:
            await update.message.reply_text("Git отключён в config.yaml")
            return
        if not await self._git.is_repo():
            await update.message.reply_text("workspace не является git-репозиторием")
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        session = self._sessions.load_session(chat_id)
        if not session.git_checkpoint:
            await update.message.reply_text("Нет точки отката для последнего сообщения.")
            return

        lock = self._sessions.lock_for(chat_id)
        if lock.locked():
            await update.message.reply_text("Подождите — сейчас выполняется задача из очереди.")
            return

        async with lock:
            try:
                msg = await self._git.rollback(session.git_checkpoint)
            except Exception as err:
                logger.exception("git rollback failed")
                await update.message.reply_text(f"❌ Откат не удался: {err}")
                return

            self._sessions.clear_git_checkpoint(chat_id)
            await update.message.reply_text(f"✅ {msg}")

    async def _send_chunks(self, update: Update, text: str) -> None:
        if not update.message:
            return
        limit = self._config.bot.max_reply_length
        for chunk in split_message(text, limit):
            await update.message.reply_text(chunk)

    async def _stream_reply(self, update: Update, stream) -> RunUpdate | None:
        if not update.message:
            return None

        status = await update.message.reply_text(
            f"⏳ {self._config.provider_label} выполняет задачу…\nПожалуйста, подождите"
        )

        async def send_text(text: str) -> None:
            await self._send_chunks(update, text)

        async def edit_status(_msg_id: str, text: str) -> None:
            await status.edit_text(text)

        async def delete_status(_msg_id: str) -> None:
            await status.delete()

        return await deliver_streamed_reply(
            stream,
            provider_label=self._config.provider_label,
            max_reply_length=self._config.bot.max_reply_length,
            stream_edit_interval_sec=self._config.bot.stream_edit_interval_sec,
            send_text=send_text,
            edit_status=edit_status,
            delete_status=delete_status,
            status_message_id=str(status.message_id),
        )

    async def _maybe_auto_commit(self, chat_id: int, user_text: str) -> str | None:
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
        logger.warning("auto-commit failed: %s", result.message)
        return None

    async def _process_user_message(self, update: Update, user_text: str) -> None:
        if not update.message:
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        prompt = f"{self._config.bot.system_prefix}\n\n{user_text}"

        lock = self._sessions.lock_for(chat_id)
        async with lock:
            if self._config.git.enabled:
                if not await self._git.is_repo():
                    await update.message.reply_text(
                        "⚠️ workspace не git-репозиторий — /undo и авто-коммит недоступны."
                    )
                else:
                    try:
                        checkpoint = await self._git.create_checkpoint(chat_id)
                        self._sessions.set_git_checkpoint(chat_id, checkpoint, user_text)
                    except Exception as err:
                        logger.exception("git checkpoint failed")
                        await update.message.reply_text(f"❌ Git checkpoint: {err}")
                        return

            await update.message.chat.send_action(ChatAction.TYPING)
            stream = self._sessions.run_prompt(chat_id, prompt, mode=self._config.mode)
            final = await self._stream_reply(update, stream)

            if final and not final.error and not final.cancelled and self._config.git.enabled:
                commit_note = await self._maybe_auto_commit(chat_id, user_text)
                if commit_note:
                    await update.message.reply_text(f"📌 {commit_note}")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        user_text = update.message.text.strip()
        lock = self._sessions.lock_for(chat_id)

        status = await self._queue.enqueue(chat_id, update, user_text, running=lock.locked())
        if status:
            await update.message.reply_text(status)

    async def post_init(self, application: Application) -> None:
        await self._sessions.start()
        logger.info("%s started for workspace %s", self._config.provider_label, self._config.workspace)
        if self._vk_bot is not None:
            self._vk_task = asyncio.create_task(self._vk_bot.poll_loop())

    async def post_shutdown(self, application: Application) -> None:
        if self._vk_task is not None:
            self._vk_task.cancel()
            self._vk_task = None
        if self._vk_bot is not None:
            await self._vk_bot.aclose()
        await self._queue.shutdown()
        await self._sessions.stop()
        logger.info("%s stopped", self._config.provider_label)

    def _build_telegram_request(self) -> HTTPXRequest:
        tg = self._config.telegram
        transport = telegram_transport() if tg.force_ipv4 else None
        return HTTPXRequest(
            connect_timeout=tg.connect_timeout,
            read_timeout=tg.read_timeout,
            write_timeout=tg.read_timeout,
            pool_timeout=tg.connect_timeout,
            proxy=tg.proxy or None,
            httpx_kwargs={"transport": transport} if transport else None,
        )

    def build_application(self) -> Application:
        request = self._build_telegram_request()
        app = (
            Application.builder()
            .token(self._config.telegram.token)
            .request(request)
            .get_updates_request(request)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("provider", self.cmd_provider))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("commit", self.cmd_commit))
        app.add_handler(CommandHandler("undo", self.cmd_undo))
        app.add_handler(CommandHandler("git", self.cmd_git))
        app.add_handler(CommandHandler("push", self.cmd_push))
        app.add_handler(CommandHandler("pull", self.cmd_pull))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        return app


def main() -> None:
    if sys.platform != "linux":
        print("cursor-linux-tg-bot работает только на Linux.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Telegram → Cursor local agent bridge (Linux only)")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    if config.telegram.force_ipv4:
        enable_ipv4_only()
        logger.info("IPv4-only mode enabled for Telegram API")

    sessions = create_session_manager(config)
    git = GitManager.from_config(config.workspace, config.git)
    vk_bot = VkCursorBot(config, sessions, git) if config.vk.enabled else None

    if config.telegram.enabled:
        bot = TelegramCursorBot(config, sessions=sessions, git=git, vk_bot=vk_bot)
        if vk_bot is not None:
            vk_bot._sessions_sync = lambda manager: setattr(bot, "_sessions", manager)
            vk_bot._extra_queues = (bot._queue,)
        bot.build_application().run_polling(drop_pending_updates=True, bootstrap_retries=-1)
    elif vk_bot is not None:
        logger.info("Telegram отключён — запускаю только VK-бота")
        asyncio.run(vk_bot.run_standalone())


if __name__ == "__main__":
    main()
