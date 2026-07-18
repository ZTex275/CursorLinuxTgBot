from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

PROVIDERS = ("cursor", "openrouter", "openrouter_cli")
PROVIDER_LABELS = {
    "cursor": "Cursor",
    "openrouter": "OpenRouter",
    "openrouter_cli": "OpenRouter CLI",
}


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
class OpenRouterCliConfig:
    api_key: str
    model: str = ""
    profile: str = "default"
    binary: str = "orc"
    mode: str = "agent"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class VoiceConfig:
    enabled: bool = True
    model: str = "tiny"
    language: str = "ru"
    device: str = "cpu"
    compute_type: str = "int8"
    show_recognized_text: bool = True


@dataclass
class BotConfig:
    welcome_message: str
    system_prefix: str
    max_reply_length: int = 4000
    stream_edit_interval_sec: float = 2.0
    max_queue_size: int = 100
    voice: VoiceConfig = field(default_factory=VoiceConfig)


@dataclass
class ServiceConfig:
    auto_restart: bool = True
    service_name: str = "cursor-linux-tg-bot"
    restart_delay_sec: float = 3.0
    pip_on_reload: bool = True


@dataclass
class GitConfig:
    enabled: bool = True
    auto_commit: bool = True
    auto_push: bool = False
    remote: str = "origin"
    branch: str = ""
    github_token: str = ""
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
    openrouter_cli: OpenRouterCliConfig | None
    bot: BotConfig
    git: GitConfig
    service: ServiceConfig
    sessions_dir: Path

    @property
    def model(self) -> str:
        if self.provider == "openrouter":
            assert self.openrouter is not None
            return self.openrouter.model
        if self.provider == "openrouter_cli":
            assert self.openrouter_cli is not None
            return self.openrouter_cli.model or f"profile:{self.openrouter_cli.profile}"
        assert self.cursor is not None
        return self.cursor.model

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    def configured_providers(self) -> list[str]:
        providers: list[str] = []
        if self.cursor is not None:
            providers.append("cursor")
        if self.openrouter is not None:
            providers.append("openrouter")
        if self.openrouter_cli is not None:
            providers.append("openrouter_cli")
        return providers


def provider_help_text() -> str:
    return "Использование: /provider cursor | openrouter | openrouter_cli"


def provider_status_text(config: AppConfig) -> str:
    available = ", ".join(config.configured_providers()) or "нет"
    return (
        f"provider: {config.provider} ({config.provider_label})\n"
        f"доступные: {available}\n"
        f"{provider_help_text()}"
    )


def model_help_text() -> str:
    return "Использование: /model <имя модели>"


def model_status_text(config: AppConfig) -> str:
    return f"model ({config.provider_label}): {config.model}\n{model_help_text()}"


def apply_model(config: AppConfig, model: str) -> str | None:
    value = model.strip()
    if not value:
        return "Укажите имя модели."

    if config.provider == "cursor":
        if config.cursor is None:
            return "Провайдер Cursor не настроен."
        config.cursor.model = value
        return None

    if config.provider == "openrouter":
        if config.openrouter is None:
            return "Провайдер OpenRouter не настроен."
        config.openrouter.model = value
        return None

    if config.openrouter_cli is None:
        return "Провайдер OpenRouter CLI не настроен."
    config.openrouter_cli.model = value
    return None


def validate_provider_choice(config: AppConfig, provider: str) -> str | None:
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        return f"Неизвестный провайдер. {provider_help_text()}"

    if normalized == "cursor" and config.cursor is None:
        return "Провайдер Cursor не настроен: задайте cursor.api_key (${CURSOR_API_KEY}) в config.yaml."
    if normalized == "openrouter" and config.openrouter is None:
        return "Провайдер OpenRouter не настроен: задайте openrouter.api_key (${OPENROUTER_API_KEY}) в config.yaml."
    if normalized == "openrouter_cli" and config.openrouter_cli is None:
        return (
            "Провайдер OpenRouter CLI не настроен: задайте openrouter_cli.api_key "
            "(${OPENROUTER_API_KEY}) в config.yaml."
        )
    return None


def _build_cursor_config(cursor: dict, workspace_path: Path, mode: str) -> CursorConfig | None:
    api_key = cursor.get("api_key", "").strip()
    if not api_key:
        return None
    return CursorConfig(
        api_key=api_key,
        model=cursor.get("model", "composer-2.5"),
        workspace=str(workspace_path),
        mode=mode,
        setting_sources=list(cursor.get("setting_sources", [])),
    )


def _build_openrouter_config(openrouter: dict, mode: str) -> OpenRouterConfig | None:
    api_key = openrouter.get("api_key", "").strip()
    if not api_key:
        return None
    return OpenRouterConfig(
        api_key=api_key,
        model=openrouter.get("model", "anthropic/claude-sonnet-4"),
        mode=mode,
        base_url=str(openrouter.get("base_url", "https://openrouter.ai/api/v1")).strip(),
        max_tool_rounds=int(openrouter.get("max_tool_rounds", 25)),
        site_url=str(openrouter.get("site_url", "")).strip(),
        app_name=str(openrouter.get("app_name", "cursor-linux-tg-bot")).strip(),
    )


def _build_openrouter_cli_config(openrouter_cli: dict, mode: str) -> OpenRouterCliConfig | None:
    api_key = openrouter_cli.get("api_key", "").strip()
    if not api_key:
        return None
    return OpenRouterCliConfig(
        api_key=api_key,
        model=str(openrouter_cli.get("model", "")).strip(),
        profile=str(openrouter_cli.get("profile", "default")).strip() or "default",
        binary=str(openrouter_cli.get("binary", "orc")).strip() or "orc",
        mode=mode,
        extra_args=[str(arg) for arg in openrouter_cli.get("extra_args", [])],
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data = _expand(raw)

    telegram = data.get("telegram", {})
    vk = data.get("vk", {}) or {}
    agent = data.get("agent", {}) or {}
    cursor = data.get("cursor", {}) or {}
    openrouter = data.get("openrouter", {}) or {}
    openrouter_cli = data.get("openrouter_cli", {}) or {}
    bot = data.get("bot", {})
    git = data.get("git", {})
    service = data.get("service", {}) or {}

    token = telegram.get("token", "").strip()
    vk_token = str(vk.get("token", "") or "").strip()
    vk_group_id = int(vk.get("group_id", 0) or 0)

    if not token and not (vk_token and vk_group_id):
        raise ValueError(
            "Нужен хотя бы один мессенджер: telegram.token (${TELEGRAM_BOT_TOKEN}) "
            "или vk.token + vk.group_id (${VK_BOT_TOKEN})"
        )

    provider = str(agent.get("provider", "cursor")).strip().lower()
    if provider not in {"cursor", "openrouter", "openrouter_cli"}:
        raise ValueError('agent.provider must be "cursor", "openrouter" or "openrouter_cli"')

    workspace = str(agent.get("workspace") or cursor.get("workspace", "")).strip()
    if not workspace:
        raise ValueError(
            "workspace is required — задайте agent.workspace или cursor.workspace "
            "(путь к Linux-директории, которой управляет агент)"
        )

    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"workspace does not exist: {workspace_path}")

    mode = str(
        agent.get("mode")
        or cursor.get("mode")
        or openrouter.get("mode")
        or openrouter_cli.get("mode", "agent")
    )
    if mode not in {"agent", "plan"}:
        raise ValueError('mode must be "agent" or "plan"')

    cursor_cfg = _build_cursor_config(cursor, workspace_path, mode)
    openrouter_cfg = _build_openrouter_config(openrouter, mode)
    openrouter_cli_cfg = _build_openrouter_cli_config(openrouter_cli, mode)

    missing = {
        "cursor": cursor_cfg is None,
        "openrouter": openrouter_cfg is None,
        "openrouter_cli": openrouter_cli_cfg is None,
    }
    if missing[provider]:
        if provider == "cursor":
            raise ValueError("cursor.api_key is required (use ${CURSOR_API_KEY} in config.yaml)")
        if provider == "openrouter":
            raise ValueError("openrouter.api_key is required (use ${OPENROUTER_API_KEY} in config.yaml)")
        raise ValueError(
            "openrouter_cli.api_key is required (use ${OPENROUTER_API_KEY} in config.yaml)"
        )

    sessions_dir = Path(data.get("sessions_dir", config_path.parent / "data" / "sessions")).expanduser()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    default_welcome = {
        "openrouter": "Отправьте сообщение — оно уйдёт в локальный OpenRouter-агент на этом сервере.",
        "openrouter_cli": "Отправьте сообщение — оно уйдёт в OpenRouter CLI (orc) на этом сервере.",
    }.get(provider, "Отправьте сообщение — оно уйдёт в локальный Cursor Agent на этом сервере.")

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
        openrouter_cli=openrouter_cli_cfg,
        bot=BotConfig(
            welcome_message=bot.get("welcome_message", default_welcome),
            system_prefix=bot.get(
                "system_prefix",
                "Пользователь управляет Linux-сервером через Telegram. Выполняй запросы на этой машине.",
            ),
            max_reply_length=int(bot.get("max_reply_length", 4000)),
            stream_edit_interval_sec=float(bot.get("stream_edit_interval_sec", 2.0)),
            max_queue_size=int(bot.get("max_queue_size", 100)),
            voice=VoiceConfig(
                enabled=bool(bot.get("voice", {}).get("enabled", True)),
                model=str(bot.get("voice", {}).get("model", "tiny")).strip() or "tiny",
                language=str(bot.get("voice", {}).get("language", "ru")).strip(),
                device=str(bot.get("voice", {}).get("device", "cpu")).strip() or "cpu",
                compute_type=str(bot.get("voice", {}).get("compute_type", "int8")).strip() or "int8",
                show_recognized_text=bool(bot.get("voice", {}).get("show_recognized_text", True)),
            ),
        ),
        git=GitConfig(
            enabled=bool(git.get("enabled", True)),
            auto_commit=bool(git.get("auto_commit", True)),
            auto_push=bool(git.get("auto_push", False)),
            remote=str(git.get("remote", "origin")).strip() or "origin",
            branch=str(git.get("branch", "")).strip(),
            github_token=str(git.get("github_token", "")).strip(),
            commit_prefix=str(git.get("commit_prefix", "tg: ")),
            max_commit_message_length=int(git.get("max_commit_message_length", 120)),
        ),
        service=ServiceConfig(
            auto_restart=bool(service.get("auto_restart", True)),
            service_name=str(service.get("service_name", "cursor-linux-tg-bot")).strip() or "cursor-linux-tg-bot",
            restart_delay_sec=float(service.get("restart_delay_sec", 3.0)),
            pip_on_reload=bool(service.get("pip_on_reload", True)),
        ),
        sessions_dir=sessions_dir,
    )
