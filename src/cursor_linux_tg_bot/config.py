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
    force_ipv4: bool = True
    proxy: str = ""
    connect_timeout: float = 30.0
    read_timeout: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.token)


@dataclass
class VkConfig:
    token: str = ""
    group_id: int = 0
    allowed_user_ids: list[int] = field(default_factory=list)
    connect_timeout: float = 30.0
    read_timeout: float = 30.0
    proxy: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.group_id)


@dataclass
class CursorConfig:
    api_key: str
    model: str
    workspace: str
    mode: str = "agent"
    setting_sources: list[str] = field(default_factory=list)


@dataclass
class OpenRouterConfig:
    api_key: str
    model: str
    mode: str = "agent"
    base_url: str = "https://openrouter.ai/api/v1"
    max_tool_rounds: int = 25
    site_url: str = ""
    app_name: str = "cursor-linux-tg-bot"


@dataclass
class BotConfig:
    welcome_message: str
    system_prefix: str
    max_reply_length: int = 4000
    stream_edit_interval_sec: float = 2.0
    max_queue_size: int = 100


@dataclass
class GitConfig:
    enabled: bool = True
    auto_commit: bool = True
    commit_prefix: str = "tg: "
    max_commit_message_length: int = 120


@dataclass
class AppConfig:
    telegram: TelegramConfig
    vk: VkConfig
    provider: str
    workspace: str
    mode: str
    cursor: CursorConfig | None
    openrouter: OpenRouterConfig | None
    bot: BotConfig
    git: GitConfig
    sessions_dir: Path

    @property
    def model(self) -> str:
        if self.provider == "openrouter":
            assert self.openrouter is not None
            return self.openrouter.model
        assert self.cursor is not None
        return self.cursor.model

    @property
    def provider_label(self) -> str:
        return "OpenRouter" if self.provider == "openrouter" else "Cursor"


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data = _expand(raw)

    telegram = data.get("telegram", {})
    vk = data.get("vk", {}) or {}
    agent = data.get("agent", {}) or {}
    cursor = data.get("cursor", {}) or {}
    openrouter = data.get("openrouter", {}) or {}
    bot = data.get("bot", {})
    git = data.get("git", {})

    token = telegram.get("token", "").strip()
    vk_token = str(vk.get("token", "") or "").strip()
    vk_group_id = int(vk.get("group_id", 0) or 0)

    if not token and not (vk_token and vk_group_id):
        raise ValueError(
            "Нужен хотя бы один мессенджер: telegram.token (${TELEGRAM_BOT_TOKEN}) "
            "или vk.token + vk.group_id (${VK_BOT_TOKEN})"
        )

    provider = str(agent.get("provider", "cursor")).strip().lower()
    if provider not in {"cursor", "openrouter"}:
        raise ValueError('agent.provider must be "cursor" or "openrouter"')

    workspace = str(agent.get("workspace") or cursor.get("workspace", "")).strip()
    if not workspace:
        raise ValueError(
            "workspace is required — задайте agent.workspace или cursor.workspace "
            "(путь к Linux-директории, которой управляет агент)"
        )

    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"workspace does not exist: {workspace_path}")

    mode = str(agent.get("mode") or cursor.get("mode") or openrouter.get("mode", "agent"))
    if mode not in {"agent", "plan"}:
        raise ValueError('mode must be "agent" or "plan"')

    cursor_cfg: CursorConfig | None = None
    openrouter_cfg: OpenRouterConfig | None = None

    if provider == "cursor":
        api_key = cursor.get("api_key", "").strip()
        if not api_key:
            raise ValueError("cursor.api_key is required (use ${CURSOR_API_KEY} in config.yaml)")
        cursor_cfg = CursorConfig(
            api_key=api_key,
            model=cursor.get("model", "composer-2.5"),
            workspace=str(workspace_path),
            mode=mode,
            setting_sources=list(cursor.get("setting_sources", [])),
        )
    else:
        api_key = openrouter.get("api_key", "").strip()
        if not api_key:
            raise ValueError("openrouter.api_key is required (use ${OPENROUTER_API_KEY} in config.yaml)")
        openrouter_cfg = OpenRouterConfig(
            api_key=api_key,
            model=openrouter.get("model", "anthropic/claude-sonnet-4"),
            mode=mode,
            base_url=str(openrouter.get("base_url", "https://openrouter.ai/api/v1")).strip(),
            max_tool_rounds=int(openrouter.get("max_tool_rounds", 25)),
            site_url=str(openrouter.get("site_url", "")).strip(),
            app_name=str(openrouter.get("app_name", "cursor-linux-tg-bot")).strip(),
        )

    sessions_dir = Path(data.get("sessions_dir", config_path.parent / "data" / "sessions")).expanduser()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    default_welcome = (
        "Отправьте сообщение — оно уйдёт в локальный OpenRouter-агент на этом сервере."
        if provider == "openrouter"
        else "Отправьте сообщение — оно уйдёт в локальный Cursor Agent на этом сервере."
    )

    return AppConfig(
        telegram=TelegramConfig(
            token=token,
            allowed_user_ids=[int(uid) for uid in telegram.get("allowed_user_ids", [])],
            force_ipv4=bool(telegram.get("force_ipv4", True)),
            proxy=str(telegram.get("proxy", "")).strip(),
            connect_timeout=float(telegram.get("connect_timeout", 30.0)),
            read_timeout=float(telegram.get("read_timeout", 30.0)),
        ),
        vk=VkConfig(
            token=vk_token,
            group_id=vk_group_id,
            allowed_user_ids=[int(uid) for uid in vk.get("allowed_user_ids", [])],
            connect_timeout=float(vk.get("connect_timeout", 30.0)),
            read_timeout=float(vk.get("read_timeout", 30.0)),
            proxy=str(vk.get("proxy", "") or "").strip(),
        ),
        provider=provider,
        workspace=str(workspace_path),
        mode=mode,
        cursor=cursor_cfg,
        openrouter=openrouter_cfg,
        bot=BotConfig(
            welcome_message=bot.get("welcome_message", default_welcome),
            system_prefix=bot.get(
                "system_prefix",
                "Пользователь управляет Linux-сервером через Telegram. Выполняй запросы на этой машине.",
            ),
            max_reply_length=int(bot.get("max_reply_length", 4000)),
            stream_edit_interval_sec=float(bot.get("stream_edit_interval_sec", 2.0)),
            max_queue_size=int(bot.get("max_queue_size", 100)),
        ),
        git=GitConfig(
            enabled=bool(git.get("enabled", True)),
            auto_commit=bool(git.get("auto_commit", True)),
            commit_prefix=str(git.get("commit_prefix", "tg: ")),
            max_commit_message_length=int(git.get("max_commit_message_length", 120)),
        ),
        sessions_dir=sessions_dir,
    )
