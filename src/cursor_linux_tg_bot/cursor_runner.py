from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from cursor_sdk import AgentOptions, AsyncClient, CursorAgentError, LocalAgentOptions, SendOptions

from .config import CursorConfig


@dataclass
class RunUpdate:
    text: str
    done: bool = False
    error: str | None = None


class CursorSessionManager:
    def __init__(self, cursor: CursorConfig, sessions_dir: Path) -> None:
        self._cursor = cursor
        self._sessions_dir = sessions_dir
        self._client: AsyncClient | None = None
        self._agents: dict[int, object] = {}
        self._locks: dict[int, asyncio.Lock] = {}

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
        self._client = await AsyncClient.launch_bridge(workspace=self._cursor.workspace).__aenter__()

    async def stop(self) -> None:
        for agent in list(self._agents.values()):
            await agent.close()
        self._agents.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def _session_file(self, chat_id: int) -> Path:
        return self._sessions_dir / f"{chat_id}.json"

    def _read_session(self, chat_id: int) -> str | None:
        path = self._session_file(chat_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("agent_id")

    def _write_session(self, chat_id: int, agent_id: str) -> None:
        self._session_file(chat_id).write_text(
            json.dumps({"agent_id": agent_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _clear_session(self, chat_id: int) -> None:
        path = self._session_file(chat_id)
        if path.exists():
            path.unlink()

    async def reset_chat(self, chat_id: int) -> None:
        agent = self._agents.pop(chat_id, None)
        if agent is not None:
            await agent.close()
        self._clear_session(chat_id)

    async def _get_or_create_agent(self, chat_id: int):
        if chat_id in self._agents:
            return self._agents[chat_id]

        assert self._client is not None
        options = self._agent_options()

        saved_id = self._read_session(chat_id)
        agent = None
        if saved_id:
            try:
                agent = await self._client.agents.resume(saved_id, options)
            except CursorAgentError:
                agent = None

        if agent is None:
            agent = await self._client.agents.create(
                model=options.model,
                api_key=options.api_key,
                local=options.local,
                mode=options.mode,
            )
            self._write_session(chat_id, agent.agent_id)

        self._agents[chat_id] = agent
        return agent

    async def run_prompt(
        self,
        chat_id: int,
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
