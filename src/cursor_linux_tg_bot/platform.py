from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

SUPPORTED_PLATFORMS = frozenset({"linux", "win32"})


def is_windows() -> bool:
    return sys.platform == "win32"


def is_linux() -> bool:
    return sys.platform == "linux"


def check_supported_platform() -> None:
    if sys.platform not in SUPPORTED_PLATFORMS:
        print(
            f"cursor-linux-tg-bot поддерживает только Linux и Windows (текущая: {sys.platform}).",
            file=sys.stderr,
        )
        sys.exit(1)


def host_label() -> str:
    return "Windows-машиной" if is_windows() else "Linux-сервером"


def default_system_prefix() -> str:
    return (
        f"Пользователь управляет этой {host_label()} через Telegram. "
        "Выполняй запросы на этой машине."
    )


def reload_agent_hint() -> str:
    if is_windows():
        return (
            "Не перезапускай сервис бота (Restart-Service, install.ps1, update.ps1) во время задачи. "
            "Если меняешь код бота — сохрани файлы и заверши ответ; бот сам перезапустится после ответа."
        )
    return (
        "Не перезапускай сервис бота (systemctl restart, ./install.sh, ./update.sh) во время задачи. "
        "Если меняешь код бота — сохрани файлы и заверши ответ; бот сам перезапустится после ответа."
    )


def venv_pip(workspace: Path) -> Path | None:
    root = workspace.resolve()
    if is_windows():
        pip = root / ".venv" / "Scripts" / "pip.exe"
    else:
        pip = root / ".venv" / "bin" / "pip"
    return pip if pip.is_file() else None


def shell_tool_description() -> str:
    if is_windows():
        return "Выполнить команду в cmd/PowerShell в рабочей директории workspace."
    return "Выполнить shell-команду в рабочей директории workspace."


def shell_tool_param_description() -> str:
    if is_windows():
        return "Команда для cmd.exe или PowerShell"
    return "Команда для bash -lc"


def voice_initial_prompt() -> str:
    if is_windows():
        return (
            "Команды для Windows: статус, перезапуск, логи, git commit, push, pull, "
            "установить программу, проверить диск, память, процессы, службы, docker."
        )
    return (
        "Команды для Linux-сервера: статус, перезапуск, логи, git commit, push, pull, "
        "установить пакет, проверить диск, память, процессы, systemd, docker, nginx."
    )


def extra_binary_paths(name: str) -> list[Path]:
    home = Path.home()
    paths: list[Path] = []
    if is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm = Path(appdata) / "npm"
            paths.extend([npm / f"{name}.cmd", npm / f"{name}.exe", npm / name])
        paths.extend(
            [
                home / ".npm-global" / f"{name}.cmd",
                home / ".npm-global" / f"{name}.exe",
                Path("C:/Program Files/nodejs") / f"{name}.cmd",
            ]
        )
    else:
        paths.extend(
            [
                home / ".npm-global" / "bin" / name,
                home / ".local" / "bin" / name,
                Path("/usr/local/bin") / name,
            ]
        )
    return paths


def resolve_binary(configured: str) -> str:
    candidate = configured.strip() or "orc"
    found = shutil.which(candidate)
    if found:
        return found
    for path in extra_binary_paths(candidate):
        if path.is_file():
            return str(path)
    if Path(candidate).is_file():
        return candidate
    return candidate


def is_binary_available(binary: str) -> bool:
    if shutil.which(binary):
        return True
    return Path(binary).is_file()


async def service_is_enabled(service_name: str) -> bool:
    if is_windows():
        proc = await asyncio.create_subprocess_exec(
            "sc",
            "query",
            service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return (await proc.wait()) == 0
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "is-enabled",
        service_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


def schedule_service_restart(
    *,
    workspace: Path,
    service_name: str,
    delay_sec: float,
    pip_install: bool,
    pip_on_reload: bool,
) -> None:
    delay = max(delay_sec, 1.0)
    if is_windows():
        parts: list[str] = []
        if pip_install and pip_on_reload:
            pip = venv_pip(workspace)
            if pip is not None:
                parts.append(f'& "{pip}" install -e "{workspace}" -q')
        parts.append(f"Restart-Service -Name '{service_name}' -Force -ErrorAction SilentlyContinue")
        ps = f"Start-Sleep -Seconds {delay:.1f}; " + "; ".join(parts)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    parts = [f"sleep {delay:.1f}"]
    if pip_install and pip_on_reload:
        pip = venv_pip(workspace)
        if pip is not None:
            parts.append(f"{pip} install -e {workspace} -q")
    parts.append(f"systemctl restart {service_name}")
    cmd = " && ".join(parts)
    subprocess.Popen(
        ["bash", "-c", cmd],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
