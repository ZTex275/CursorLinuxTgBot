from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from .git_manager import GitCheckpoint
from .session_store import ChatSession, ChatKey


@dataclass
class RunUpdate:
    text: str
    done: bool = False
    error: str | None = None
    cancelled: bool = False
    stage: str | None = None


class AgentSessionManager(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def lock_for(self, chat_id: ChatKey) -> asyncio.Lock: ...

    def load_session(self, chat_id: ChatKey) -> ChatSession: ...

    def save_session(self, chat_id: ChatKey, session: ChatSession) -> None: ...

    def set_git_checkpoint(
        self,
        chat_id: ChatKey,
        checkpoint: GitCheckpoint | None,
        user_message: str | None = None,
    ) -> None: ...

    def clear_git_checkpoint(self, chat_id: ChatKey) -> None: ...

    async def reset_chat(self, chat_id: ChatKey) -> None: ...

    async def run_prompt(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[RunUpdate]: ...

    async def cancel_active(self, chat_id: ChatKey) -> bool: ...
