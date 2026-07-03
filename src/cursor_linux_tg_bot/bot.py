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

from .config import AppConfig, load_config
from .cursor_runner import CursorSessionManager, RunUpdate
from .git_manager import GitManager

logger = logging.getLogger(__name__)


def _split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


class TelegramCursorBot:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._sessions = CursorSessionManager(config.cursor, config.sessions_dir)
        self._git = GitManager(
            config.cursor.workspace,
            enabled=config.git.enabled,
        )

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
            await update.message.reply_text("Новая сессия Cursor. История и git-чекпоинт сброшены.")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        if not context.args or context.args[0] not in {"agent", "plan"}:
            await update.message.reply_text("Использование: /mode agent | plan")
            return
        self._config.cursor.mode = context.args[0]
        await update.message.reply_text(f"Режим по умолчанию: {context.args[0]}")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message:
            return
        cursor = self._config.cursor
        git_on = self._config.git.enabled and await self._git.is_repo()
        text = textwrap.dedent(
            f"""
            workspace: {cursor.workspace}
            model: {cursor.model}
            mode: {cursor.mode}
            git: {"включён" if git_on else "выкл / не репозиторий"}
            auto_commit: {self._config.git.auto_commit}
            """
        ).strip()
        await update.message.reply_text(text)

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
            await update.message.reply_text(f"✅ {result.message}")
        else:
            await update.message.reply_text(f"ℹ️ {result.message}")

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
            await update.message.reply_text("Подождите — предыдущий запрос ещё выполняется.")
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
        for chunk in _split_message(text, limit):
            await update.message.reply_text(chunk)

    async def _stream_reply(self, update: Update, stream) -> RunUpdate | None:
        if not update.message:
            return None

        status = await update.message.reply_text("⏳ Cursor думает…")
        last_edit = 0.0
        interval = self._config.bot.stream_edit_interval_sec
        limit = self._config.bot.max_reply_length
        final_item: RunUpdate | None = None

        async for item in stream:
            final_item = item
            if item.error:
                await status.edit_text(f"❌ {item.error}")
                return final_item

            now = asyncio.get_event_loop().time()
            if not item.done and now - last_edit < interval:
                continue
            last_edit = now

            preview = item.text.strip() or "…"
            if len(preview) > limit:
                preview = preview[: limit - 1] + "…"
            try:
                if item.done:
                    await status.delete()
                    await self._send_chunks(update, item.text or "Готово.")
                else:
                    await status.edit_text(preview)
            except Exception:
                if item.done:
                    await self._send_chunks(update, item.text or "Готово.")

        return final_item

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
            return result.message
        logger.warning("auto-commit failed: %s", result.message)
        return None

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        user_text = update.message.text.strip()
        prompt = f"{self._config.bot.system_prefix}\n\n{user_text}"

        lock = self._sessions.lock_for(chat_id)
        if lock.locked():
            await update.message.reply_text("Подождите — предыдущий запрос ещё выполняется.")
            return

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
            stream = self._sessions.run_prompt(chat_id, prompt, mode=self._config.cursor.mode)
            final = await self._stream_reply(update, stream)

            if final and not final.error and self._config.git.enabled:
                commit_note = await self._maybe_auto_commit(chat_id, user_text)
                if commit_note and update.message:
                    await update.message.reply_text(f"📌 {commit_note}")

    async def post_init(self, application: Application) -> None:
        await self._sessions.start()
        logger.info("Cursor bridge started for workspace %s", self._config.cursor.workspace)

    async def post_shutdown(self, application: Application) -> None:
        await self._sessions.stop()
        logger.info("Cursor bridge stopped")

    def build_application(self) -> Application:
        app = (
            Application.builder()
            .token(self._config.telegram.token)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("commit", self.cmd_commit))
        app.add_handler(CommandHandler("undo", self.cmd_undo))
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
    bot = TelegramCursorBot(config)
    bot.build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
