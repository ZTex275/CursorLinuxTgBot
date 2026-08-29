from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import AsyncIterator

from cursor_sdk import AgentOptions, AsyncAgent, AsyncClient, CursorAgentError, LocalAgentOptions, SendOptions
from cursor_sdk.errors import AgentNotFoundError, InternalServerError, NetworkError, NotFoundError

from .agent_base import RunUpdate
from .config import CursorConfig
from .context_compact import (
    is_compactable_run_error,
    is_stale_session_run_error,
    merge_summaries,
    needs_compaction,
    summary_from_agent_messages,
    wrap_prompt_with_summary,
)
from .git_manager import GitCheckpoint
from .session_store import ChatKey, ChatSession, SessionStore
from .textutil import stage_from_sdk_message

logger = logging.getLogger(__name__)


def _is_recoverable_agent_error(err: CursorAgentError) -> bool:
    if isinstance(err, (AgentNotFoundError, InternalServerError, NetworkError, NotFoundError)):
        return True
    code = (err.code or err.proto_error_code or "").lower()
    return code in {
        "agent_not_found",
        "internal",
        "internal_error",
        "internal_server_error",
        "unavailable",
        "upstream_error",
        "not_found",
    }


class CursorSessionManager:
    def __init__(self, cursor: CursorConfig, sessions_dir: Path) -> None:
        self._cursor = cursor
        self._sessions = SessionStore(sessions_dir)
        self._client: AsyncClient | None = None
        self._agents: dict[ChatKey, object] = {}
        self._agent_last_used: dict[ChatKey, float] = {}
        self._locks: dict[ChatKey, asyncio.Lock] = {}
        self._active_runs: dict[ChatKey, object] = {}

    def _agent_options(self) -> AgentOptions:
        local = LocalAgentOptions(
            cwd=self._cursor.workspace,
            setting_sources=self._cursor.setting_sources or None,
        )
        return AgentOptions(
            model=self._cursor.model,
            api_key=self._cursor.api_key,
            local=local,
            mode=self._cursor.mode,
        )

    async def start(self) -> None:
        self._client = await AsyncClient.launch_bridge(workspace=self._cursor.workspace)

    async def stop(self) -> None:
        for agent in list(self._agents.values()):
            await agent.close()
        self._agents.clear()
        self._agent_last_used.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def lock_for(self, chat_id: ChatKey) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def load_session(self, chat_id: ChatKey) -> ChatSession:
        return self._sessions.load(chat_id)

    def save_session(self, chat_id: ChatKey, session: ChatSession) -> None:
        self._sessions.save(chat_id, session)

    def set_git_checkpoint(self, chat_id: ChatKey, checkpoint: GitCheckpoint | None, user_message: str | None = None) -> None:
        session = self._sessions.load(chat_id)
        session.git_checkpoint = checkpoint
        if user_message is not None:
            session.last_user_message = user_message
        self._sessions.save(chat_id, session)

    def clear_git_checkpoint(self, chat_id: ChatKey) -> None:
        session = self._sessions.load(chat_id)
        session.git_checkpoint = None
        self._sessions.save(chat_id, session)

    async def reset_chat(self, chat_id: ChatKey) -> None:
        await self._discard_agent(chat_id)
        self._sessions.clear(chat_id)

    async def invalidate_for_model_change(self) -> None:
        for chat_id in list(self._agents.keys()):
            await self._discard_agent(chat_id)

    async def cancel_active(self, chat_id: ChatKey) -> bool:
        run = self._active_runs.pop(chat_id, None)
        if run is None:
            return False
        try:
            await run.cancel()
        except Exception:
            logger.exception("cancel cursor run failed chat_id=%s", chat_id)
        return True

    async def _discard_agent(self, chat_id: ChatKey, *, clear_agent_id: bool = True) -> None:
        agent = self._agents.pop(chat_id, None)
        self._agent_last_used.pop(chat_id, None)
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                pass
        if not clear_agent_id:
            return
        session = self._sessions.load(chat_id)
        if session.agent_id:
            session.agent_id = None
            self._sessions.save(chat_id, session)

    async def _restart_bridge(self) -> None:
        logger.warning("Перезапуск Cursor bridge")
        for agent in list(self._agents.values()):
            try:
                await agent.close()
            except Exception:
                pass
        self._agents.clear()
        self._agent_last_used.clear()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._client = await AsyncClient.launch_bridge(workspace=self._cursor.workspace)

    async def _capture_summary_before_agent_loss(self, chat_id: ChatKey, agent_id: str) -> None:
        session = self._sessions.load(chat_id)
        summary_parts: list[str | None] = [session.context_summary]
        if self._client is None:
            return

        try:
            info = await AsyncAgent.get(
                agent_id,
                client=self._client,
                cwd=self._cursor.workspace,
                api_key=self._cursor.api_key,
            )
            if info.summary.strip():
                summary_parts.append(info.summary.strip())
        except Exception:
            logger.debug("GetAgent summary unavailable for lost agent %s", agent_id, exc_info=True)

        combined = merge_summaries(*summary_parts)
        if not combined:
            return
        session.context_summary = combined
        self._sessions.save(chat_id, session)

    async def _get_or_create_agent(self, chat_id: ChatKey):
        cached = self._agents.get(chat_id)
        last_used = self._agent_last_used.get(chat_id, 0.0)
        idle_sec = time.monotonic() - last_used
        if cached is not None and idle_sec < self._cursor.agent_idle_refresh_sec:
            return cached
        if cached is not None:
            logger.info(
                "Переподключение к агенту chat_id=%s после простоя %.0f с",
                chat_id,
                idle_sec,
            )
            await self._discard_agent(chat_id, clear_agent_id=False)

        assert self._client is not None
        options = self._agent_options()
        session = self._sessions.load(chat_id)

        agent = None
        old_agent_id = session.agent_id
        if old_agent_id:
            try:
                agent = await self._client.agents.resume(old_agent_id, options)
            except CursorAgentError as err:
                logger.warning(
                    "Не удалось возобновить агента %s для chat_id=%s: %s",
                    old_agent_id,
                    chat_id,
                    err.message,
                )
                await self._capture_summary_before_agent_loss(chat_id, old_agent_id)
                agent = None

        if agent is None:
            agent = await self._client.agents.create(options)
            session = self._sessions.load(chat_id)
            session.agent_id = agent.agent_id
            session.turn_count = 0
            session.last_input_tokens = 0
            if old_agent_id and old_agent_id != agent.agent_id:
                logger.info(
                    "Создан новый агент для chat_id=%s (старый %s недоступен)",
                    chat_id,
                    old_agent_id,
                )
            self._sessions.save(chat_id, session)

        self._agents[chat_id] = agent
        self._agent_last_used[chat_id] = time.monotonic()
        return agent

    async def _resolve_agent_for_summary(self, chat_id: ChatKey):
        agent = self._agents.get(chat_id)
        if agent is not None:
            return agent

        session = self._sessions.load(chat_id)
        if not session.agent_id or self._client is None:
            return None

        try:
            agent = await self._client.agents.resume(session.agent_id, self._agent_options())
            self._agents[chat_id] = agent
            return agent
        except CursorAgentError:
            return None

    async def _fetch_agent_summary(self, agent) -> str:
        summary_parts: list[str] = []

        try:
            info = await AsyncAgent.get(
                agent.agent_id,
                client=agent.client,
                cwd=self._cursor.workspace,
                api_key=self._cursor.api_key,
            )
            if info.summary.strip():
                summary_parts.append(info.summary.strip())
        except Exception:
            logger.debug("GetAgent summary unavailable for %s", agent.agent_id, exc_info=True)

        try:
            messages = await agent.list_messages()
            derived = summary_from_agent_messages(messages)
            if derived:
                summary_parts.append(derived)
        except Exception:
            logger.debug("ListAgentMessages unavailable for %s", agent.agent_id, exc_info=True)

        return merge_summaries(*summary_parts)

    async def _compact_session(self, chat_id: ChatKey) -> str | None:
        session = self._sessions.load(chat_id)
        summary_parts: list[str | None] = [session.context_summary]

        agent = await self._resolve_agent_for_summary(chat_id)
        if agent is not None:
            fresh_summary = await self._fetch_agent_summary(agent)
            if fresh_summary:
                summary_parts.append(fresh_summary)

        await self._discard_agent(chat_id)

        combined = merge_summaries(*summary_parts)
        session = self._sessions.load(chat_id)
        session.turn_count = 0
        session.last_input_tokens = 0
        session.context_summary = combined or None
        self._sessions.save(chat_id, session)

        if combined:
            logger.info(
                "Контекст сжат для chat_id=%s, summary=%s символов",
                chat_id,
                len(combined),
            )
        else:
            logger.info("Контекст сброшен для chat_id=%s (summary пустой)", chat_id)
        return combined or None

    async def _prepare_prompt(self, chat_id: ChatKey, prompt: str) -> str:
        session = self._sessions.load(chat_id)
        if needs_compaction(session, self._cursor):
            logger.info(
                "Переполнение контекста chat_id=%s: turns=%s tokens=%s",
                chat_id,
                session.turn_count,
                session.last_input_tokens,
            )
            summary = await self._compact_session(chat_id)
            return wrap_prompt_with_summary(summary, prompt)
        if session.context_summary and session.turn_count == 0:
            return wrap_prompt_with_summary(session.context_summary, prompt)
        return prompt

    def _record_successful_run(self, chat_id: ChatKey, result) -> None:
        self._agent_last_used[chat_id] = time.monotonic()
        session = self._sessions.load(chat_id)
        session.turn_count += 1
        usage = getattr(result, "usage", None)
        if usage is not None and usage.input_tokens:
            session.last_input_tokens = usage.input_tokens
        self._sessions.save(chat_id, session)

    async def _start_run(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None,
    ):
        send_options = SendOptions(mode=mode) if mode else None

        for attempt in range(2):
            try:
                agent = await self._get_or_create_agent(chat_id)
                return await agent.send(prompt, send_options)
            except CursorAgentError as err:
                if attempt == 0 and _is_recoverable_agent_error(err):
                    logger.warning(
                        "Сбой агента (%s), пересоздаю сессию для %s",
                        err.message,
                        chat_id,
                    )
                    if isinstance(err, NetworkError):
                        await self._restart_bridge()
                    await self._discard_agent(chat_id)
                    continue
                raise

        raise RuntimeError("Cursor не запустился: internal error")

    async def _stream_run(self, chat_id: ChatKey, run) -> AsyncIterator[RunUpdate]:
        buffer = ""
        last_emit = 0.0
        current_stage = "Подключение к агенту"
        current_detail: str | None = None

        async for message in run.messages():
            stage_info = stage_from_sdk_message(message)
            if stage_info:
                current_stage, current_detail = stage_info
                yield RunUpdate(
                    text=buffer,
                    done=False,
                    stage=current_stage,
                    detail=current_detail,
                )

            if message.type != "assistant":
                continue
            for block in message.message.content:
                if block.type != "text":
                    continue
                buffer += block.text
                now = time.monotonic()
                if now - last_emit >= 0.5:
                    last_emit = now
                    yield RunUpdate(
                        text=buffer,
                        done=False,
                        stage=current_stage or "Формирую ответ",
                        detail=current_detail,
                    )

        result = await run.wait()
        if result.status == "cancelled":
            yield RunUpdate(text=buffer.strip() or "Остановлено.", done=True, cancelled=True)
            return

        if result.status == "error":
            detail = (result.result or "").strip()
            error_text = detail or "Агент завершился с ошибкой."
            logger.warning("Агент завершился с ошибкой chat_id=%s: %s", chat_id, error_text)
            yield RunUpdate(text=buffer, done=True, error=error_text)
            return

        final_text = buffer.strip()
        if not final_text:
            try:
                final_text = (await run.text()).strip()
            except Exception:
                final_text = "Готово (без текстового ответа)."

        self._record_successful_run(chat_id, result)
        yield RunUpdate(text=final_text or "Готово.", done=True)

    async def run_prompt(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[RunUpdate]:
        session = self._sessions.load(chat_id)
        if needs_compaction(session, self._cursor):
            yield RunUpdate(text="", done=False, stage="Сжимаю контекст сессии")

        effective_prompt = await self._prepare_prompt(chat_id, prompt)

        for compact_attempt in range(2):
            run = None
            try:
                yield RunUpdate(text="", done=False, stage="Запуск агента")
                run = await self._start_run(chat_id, effective_prompt, mode=mode)
            except CursorAgentError as err:
                yield RunUpdate(text="", done=True, error=f"Cursor не запустился: {err.message}")
                return
            except RuntimeError as err:
                yield RunUpdate(text="", done=True, error=str(err))
                return

            self._active_runs[chat_id] = run
            terminal_result = None
            try:
                async for update in self._stream_run(chat_id, run):
                    if update.done:
                        terminal_result = update
                    yield update
            finally:
                self._active_runs.pop(chat_id, None)

            if terminal_result is None:
                return
            if terminal_result.cancelled or not terminal_result.error:
                return

            if compact_attempt == 0 and terminal_result.error:
                if is_stale_session_run_error(terminal_result.error):
                    logger.warning(
                        "Устаревшая сессия (%s), переподключаю агента для chat_id=%s",
                        terminal_result.error,
                        chat_id,
                    )
                    yield RunUpdate(text="", done=False, stage="Обновляю сессию и повторяю")
                    await self._discard_agent(chat_id, clear_agent_id=False)
                    continue
                if is_compactable_run_error(terminal_result.error):
                    logger.warning(
                        "Ошибка агента (%s), сжимаю контекст и повторяю для chat_id=%s",
                        terminal_result.error,
                        chat_id,
                    )
                    yield RunUpdate(text="", done=False, stage="Сжимаю контекст и повторяю")
                    summary = await self._compact_session(chat_id)
                    effective_prompt = wrap_prompt_with_summary(summary, prompt)
                    continue
            return
