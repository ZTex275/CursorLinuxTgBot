from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx

from .config import AppConfig
from .platform import is_binary_available, is_windows, resolve_binary

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_SLASH_MODEL_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:/+#-]*)\s*(?:[-–—|:]\s*)?(.*)$"
)
_PLAIN_MODEL_RE = re.compile(
    r"^([a-z0-9][a-z0-9_.+/#:-]*)\s*(?:[-–—|:]\s*)?(.*)$"
)


@dataclass
class ModelInfo:
    id: str
    name: str = ""


def _strip_ansi(text: str) -> str:
    return _OSC_RE.sub("", _ANSI_RE.sub("", text))


def _parse_cli_models(raw: str, *, require_slash: bool) -> list[ModelInfo]:
    pattern = _SLASH_MODEL_RE if require_slash else _PLAIN_MODEL_RE
    models: list[ModelInfo] = []
    seen: set[str] = set()

    for raw_line in _strip_ansi(raw).splitlines():
        line = raw_line.strip().lstrip("-*•> ").strip()
        if not line or line.endswith(":"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        model_id = match.group(1).rstrip(",;")
        if model_id in seen:
            continue
        seen.add(model_id)
        name = match.group(2).strip(" -–—|:\t")
        models.append(ModelInfo(model_id, name))
    return models


async def _run_cli(binary: str, args: list[str], *, cwd: str, timeout: float = 60.0) -> str:
    if not is_binary_available(binary):
        raise RuntimeError(f"CLI не найден: {binary}")

    cmd = [binary, *args]
    if is_windows() and binary.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", *cmd]

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"команда {' '.join(args)} не ответила за {timeout:.0f}с")

    text = _strip_ansi(output.decode("utf-8", errors="replace")).strip()
    if proc.returncode != 0:
        raise RuntimeError(text[-800:] or f"код {proc.returncode}")
    return text


async def _openrouter_models(
    base_url: str,
    api_key: str,
) -> list[ModelInfo]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    models: list[ModelInfo] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "") or "").strip()
            if not model_id:
                continue
            name = str(item.get("name", "") or "").strip()
            pricing = item.get("pricing") or {}
            try:
                is_free = (
                    float(pricing.get("prompt", 1)) == 0
                    and float(pricing.get("completion", 1)) == 0
                )
            except (TypeError, ValueError):
                is_free = False
            if is_free and "free" not in name.lower():
                name = f"{name} · free".strip(" ·")
            models.append(ModelInfo(model_id, name))
    return models


async def _models_for(config: AppConfig) -> list[ModelInfo]:
    if config.provider == "opencode_cli":
        assert config.opencode_cli is not None
        binary = resolve_binary(config.opencode_cli.binary)
        raw = await _run_cli(binary, ["models"], cwd=config.workspace)
        models = _parse_cli_models(raw, require_slash=True)
        if not models:
            raise RuntimeError(
                "OpenCode не вернул модели. Проверьте `opencode auth login` и доступность провайдера."
            )
        return models

    if config.provider == "cursor":
        binary = resolve_binary("agent")
        raw = await _run_cli(binary, ["models"], cwd=config.workspace)
        models = _parse_cli_models(raw, require_slash=False)
        if not models:
            raise RuntimeError("Cursor CLI не вернул модели. Проверьте `agent login`.")
        return models

    if config.provider == "openrouter":
        assert config.openrouter is not None
        return await _openrouter_models(config.openrouter.base_url, config.openrouter.api_key)

    assert config.openrouter_cli is not None
    return await _openrouter_models(
        "https://openrouter.ai/api/v1",
        config.openrouter_cli.api_key,
    )


def _format_models(
    models: list[ModelInfo],
    *,
    provider_label: str,
    query: str | None,
    max_models: int = 60,
    max_chars: int = 3500,
) -> str:
    if query:
        needle = query.lower()
        models = [
            model
            for model in models
            if needle in model.id.lower() or needle in model.name.lower()
        ]

    title = f"Модели {provider_label}"
    if query:
        title += f" по запросу «{query}»"

    if not models:
        return (
            f"{title}: ничего не найдено.\n"
            "Попробуйте другой фильтр или проверьте авторизацию провайдера."
        )

    header = f"{title} — найдено {len(models)}:"
    lines: list[str] = []
    used = len(header)
    for model in models:
        line = model.id if not model.name else f"{model.id} — {model.name}"
        if used + len(line) + 1 > max_chars - 120:
            break
        lines.append(line)
        used += len(line) + 1

    if len(models) == 1:
        footer = [f"Установить: /model {models[0].id}"]
    else:
        footer = [f"Например: /model {models[0].id}"]
    if len(models) > len(lines):
        footer.append(f"Показаны первые {len(lines)}. Уточните фильтром: /models {query or '<текст>'}")
    elif not query:
        footer.append("Фильтр: /models deepseek")

    return "\n".join([header, *lines, "", *footer])


async def list_models(config: AppConfig, query: str | None = None) -> str:
    try:
        models = await _models_for(config)
    except Exception as err:
        logger.warning("failed to list models for %s: %s", config.provider, err)
        return (
            f"Не удалось получить список моделей {config.provider_label}: {err}\n"
            "Проверьте установку CLI и авторизацию (например, `opencode auth login`)."
        )

    return _format_models(
        models,
        provider_label=config.provider_label,
        query=query,
    )
