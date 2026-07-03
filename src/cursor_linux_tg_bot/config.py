from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, "")

    return _ENV_PATTERN.sub(repl, value)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass
class TelegramConfig:
    token: str
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class CursorConfig:
    api_key: str
    model: str
    workspace: str
    mode: str = "agent"
    setting_sources: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    welcome_message: str
    system_prefix: str
    max_reply_length: int = 4000
    stream_edit_interval_sec: float = 2.0


@dataclass
class GitConfig:
    enabled: bool = True
    auto_commit: bool = True
    commit_prefix: str = "tg: "
    max_commit_message_length: int = 120


@dataclass
class AppConfig:
    telegram: TelegramConfig
    cursor: CursorConfig
    bot: BotConfig
    git: GitConfig
    sessions_dir: Path


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data = _expand(raw)

    telegram = data.get("telegram", {})
    cursor = data.get("cursor", {})
    bot = data.get("bot", {})
    git = data.get("git", {})

    token = telegram.get("token", "").strip()
    api_key = cursor.get("api_key", "").strip()
    workspace = cursor.get("workspace", "").strip()

    if not token:
        raise ValueError("telegram.token is required (use ${TELEGRAM_BOT_TOKEN} in config.yaml)")
    if not api_key:
        raise ValueError("cursor.api_key is required (use ${CURSOR_API_KEY} in config.yaml)")
    if not workspace:
        raise ValueError("cursor.workspace is required — path to the Linux workspace the agent controls")

    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"cursor.workspace does not exist: {workspace_path}")

    sessions_dir = Path(data.get("sessions_dir", config_path.parent / "data" / "sessions")).expanduser()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    mode = cursor.get("mode", "agent")
    if mode not in {"agent", "plan"}:
        raise ValueError('cursor.mode must be "agent" or "plan"')

    return AppConfig(
        telegram=TelegramConfig(
            token=token,
            allowed_user_ids=[int(uid) for uid in telegram.get("allowed_user_ids", [])],
        ),
        cursor=CursorConfig(
            api_key=api_key,
            model=cursor.get("model", "composer-2.5"),
            workspace=str(workspace_path),
            mode=mode,
            setting_sources=list(cursor.get("setting_sources", [])),
        ),
        bot=BotConfig(
            welcome_message=bot.get(
                "welcome_message",
                "Отправьте сообщение — оно уйдёт в локальный Cursor Agent на этом сервере.",
            ),
            system_prefix=bot.get(
                "system_prefix",
                "Пользователь управляет Linux-сервером через Telegram. Выполняй запросы на этой машине.",
            ),
            max_reply_length=int(bot.get("max_reply_length", 4000)),
            stream_edit_interval_sec=float(bot.get("stream_edit_interval_sec", 2.0)),
        ),
        git=GitConfig(
            enabled=bool(git.get("enabled", True)),
            auto_commit=bool(git.get("auto_commit", True)),
            commit_prefix=str(git.get("commit_prefix", "tg: ")),
            max_commit_message_length=int(git.get("max_commit_message_length", 120)),
        ),
        sessions_dir=sessions_dir,
    )
