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
from .cursor_runner import CursorSessionManager

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
            await update.message.reply_text("Новая сессия Cursor. История сброшена.")

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
        text = textwrap.dedent(
            f"""
            workspace: {cursor.workspace}
            model: {cursor.model}
            mode: {cursor.mode}
            """
        ).strip()
        await update.message.reply_text(text)

    async def _send_chunks(self, update: Update, text: str) -> None:
        if not update.message:
            return
        limit = self._config.bot.max_reply_length
        for chunk in _split_message(text, limit):
            await update.message.reply_text(chunk)

    async def _stream_reply(self, update: Update, stream) -> None:
        if not update.message:
            return

        status = await update.message.reply_text("⏳ Cursor думает…")
        last_edit = 0.0
        interval = self._config.bot.stream_edit_interval_sec
        limit = self._config.bot.max_reply_length

        async for item in stream:
            if item.error:
                await status.edit_text(f"❌ {item.error}")
                return

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

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            await self._deny(update)
            return
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id if update.effective_chat else 0
        prompt = f"{self._config.bot.system_prefix}\n\n{update.message.text.strip()}"

        lock = self._sessions.lock_for(chat_id)
        if lock.locked():
            await update.message.reply_text("Подождите — предыдущий запрос ещё выполняется.")
            return

        async with lock:
            await update.message.chat.send_action(ChatAction.TYPING)
            stream = self._sessions.run_prompt(chat_id, prompt, mode=self._config.cursor.mode)
            await self._stream_reply(update, stream)

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
