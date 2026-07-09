from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from cursor_sdk import AgentOptions, AsyncClient, CursorAgentError, LocalAgentOptions, SendOptions

from .config import CursorConfig
from .git_manager import GitCheckpoint
from .session_store import ChatSession, SessionStore

ChatKey = int | str


@dataclass
class RunUpdate:
    text: str
    done: bool = False
    error: str | None = None


class CursorSessionManager:
    def __init__(self, cursor: CursorConfig, sessions_dir: Path) -> None:
        self._cursor = cursor
        self._sessions = SessionStore(sessions_dir)
        self._client: AsyncClient | None = None
        self._agents: dict[ChatKey, object] = {}
        self._locks: dict[ChatKey, asyncio.Lock] = {}

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
        agent = self._agents.pop(chat_id, None)
        if agent is not None:
            await agent.close()
        self._sessions.clear(chat_id)

    async def _get_or_create_agent(self, chat_id: ChatKey):
        if chat_id in self._agents:
            return self._agents[chat_id]

        assert self._client is not None
        options = self._agent_options()
        session = self._sessions.load(chat_id)

        agent = None
        if session.agent_id:
            try:
                agent = await self._client.agents.resume(session.agent_id, options)
            except CursorAgentError:
                agent = None

        if agent is None:
            agent = await self._client.agents.create(
                model=options.model,
                api_key=options.api_key,
                local=options.local,
                mode=options.mode,
            )
            session.agent_id = agent.agent_id
            self._sessions.save(chat_id, session)

        self._agents[chat_id] = agent
        return agent

    async def run_prompt(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[RunUpdate]:
        agent = await self._get_or_create_agent(chat_id)
        send_options = SendOptions(mode=mode) if mode else None

        try:
            run = await agent.send(prompt, send_options)
        except CursorAgentError as err:
            yield RunUpdate(text="", done=True, error=f"Cursor не запустился: {err.message}")
            return

        buffer = ""
        last_emit = 0.0

        async for message in run.messages():
            if message.type != "assistant":
                continue
            for block in message.message.content:
                if block.type != "text":
                    continue
                buffer += block.text
                now = time.monotonic()
                if now - last_emit >= 0.5:
                    last_emit = now
                    yield RunUpdate(text=buffer, done=False)

        result = await run.wait()
        if result.status == "error":
            yield RunUpdate(text=buffer, done=True, error="Агент завершился с ошибкой.")
            return

        final_text = buffer.strip()
        if not final_text:
            try:
                final_text = (await run.text()).strip()
            except Exception:
                final_text = "Готово (без текстового ответа)."

        yield RunUpdate(text=final_text or "Готово.", done=True)
