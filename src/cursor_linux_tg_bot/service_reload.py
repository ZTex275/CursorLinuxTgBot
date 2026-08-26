from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_base import RunUpdate
from .config import ServiceConfig
from .git_manager import GitManager
from .platform import is_windows, reload_agent_hint, schedule_service_restart, service_is_enabled

logger = logging.getLogger(__name__)

RELOAD_AGENT_HINT = reload_agent_hint()

_BOT_RELOAD_PREFIXES = (
    "src/cursor_linux_tg_bot/",
    "pyproject.toml",
    "run.py",
    "config.yaml",
)


def is_bot_workspace(workspace: str | Path) -> bool:
    root = Path(workspace).resolve()
    return (root / "src" / "cursor_linux_tg_bot" / "bot.py").is_file()


def is_bot_reload_path(path: str) -> bool:
    normalized = path.strip().lstrip("./")
    if not normalized:
        return False
    for prefix in _BOT_RELOAD_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix):
            return True
    return False


def augment_prompt(system_prefix: str, user_text: str, *, include_reload_hint: bool) -> str:
    prefix = system_prefix.rstrip()
    if include_reload_hint:
        prefix = f"{prefix}\n{RELOAD_AGENT_HINT}"
    return f"{prefix}\n\n{user_text}"


class BotReloader:
    def __init__(self, workspace: str, cfg: ServiceConfig) -> None:
        self._workspace = Path(workspace).resolve()
        self._cfg = cfg

    def enabled(self) -> bool:
        return self._cfg.auto_restart and is_bot_workspace(self._workspace)

    async def snapshot_sha(self, git: GitManager) -> str | None:
        if not await git.is_repo():
            return None
        return await git._head_sha()

    async def changed_bot_files(self, git: GitManager, since_sha: str) -> list[str]:
        code, stdout, _ = await git._git("diff", "--name-only", since_sha)
        if code != 0:
            return []
        return [path for path in stdout.splitlines() if is_bot_reload_path(path)]

    @staticmethod
    def needs_pip_install(changed_files: list[str]) -> bool:
        return any(path.endswith("pyproject.toml") for path in changed_files)

    async def _service_managed(self) -> bool:
        return await service_is_enabled(self._cfg.service_name)

    def schedule_restart(self, *, pip_install: bool) -> None:
        logger.info(
            "Запланирован перезапуск бота через %.1f с (service=%s, pip=%s)",
            self._cfg.restart_delay_sec,
            self._cfg.service_name,
            pip_install,
        )
        schedule_service_restart(
            workspace=self._workspace,
            service_name=self._cfg.service_name,
            delay_sec=self._cfg.restart_delay_sec,
            pip_install=pip_install,
            pip_on_reload=self._cfg.pip_on_reload,
        )

    async def maybe_restart_after_task(
        self,
        *,
        git: GitManager,
        start_sha: str | None,
        final: RunUpdate | None,
        notify: Callable[[str], Awaitable[None]],
    ) -> None:
        if not self.enabled() or not start_sha or not final or final.error or final.cancelled:
            return

        changed = await self.changed_bot_files(git, start_sha)
        if not changed:
            return

        if not await self._service_managed():
            backend = "служба Windows" if is_windows() else "systemd-сервис"
            logger.warning(
                "Код бота изменён (%s), но %s %s не найден — перезапуск пропущен",
                ", ".join(changed),
                backend,
                self._cfg.service_name,
            )
            await notify("Код бота изменён. Перезапустите сервис вручную, чтобы применить изменения.")
            return

        await notify("Изменения кода бота сохранены. Перезапуск через несколько секунд для применения.")
        self.schedule_restart(pip_install=self.needs_pip_install(changed))
