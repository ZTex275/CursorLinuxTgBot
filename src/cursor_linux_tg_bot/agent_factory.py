from __future__ import annotations

from pathlib import Path

from .agent_base import AgentSessionManager
from .config import AppConfig
from .cursor_runner import CursorSessionManager
from .openrouter_runner import OpenRouterSessionManager


def create_session_manager(config: AppConfig) -> AgentSessionManager:
    if config.provider == "openrouter":
        assert config.openrouter is not None
        return OpenRouterSessionManager(
            config.openrouter,
            workspace=config.workspace,
            sessions_dir=config.sessions_dir,
            system_prefix=config.bot.system_prefix,
        )
    assert config.cursor is not None
    return CursorSessionManager(config.cursor, config.sessions_dir)
