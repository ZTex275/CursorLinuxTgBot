from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import AppConfig

logger = logging.getLogger(__name__)

_MODES = {"agent", "plan"}


class RuntimeSettings:
    """Persist provider/model/mode selected from chat across restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to read runtime settings: %s", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception:
            logger.exception("failed to save runtime settings: %s", self._path)

    def apply(self, config: AppConfig) -> None:
        provider = str(self._data.get("provider", "") or "").strip().lower()
        if provider:
            if provider in config.configured_providers():
                config.provider = provider
            else:
                logger.warning(
                    "saved provider %s is not configured; keeping %s",
                    provider,
                    config.provider,
                )

        mode = str(self._data.get("mode", "") or "").strip().lower()
        if mode in _MODES:
            config.mode = mode

        models = self._data.get("models")
        if isinstance(models, dict):
            self._apply_models(config, models)

        self._sync_modes(config)

    @staticmethod
    def _apply_models(config: AppConfig, models: dict[str, Any]) -> None:
        targets = {
            "cursor": config.cursor,
            "openrouter": config.openrouter,
            "openrouter_cli": config.openrouter_cli,
            "opencode_cli": config.opencode_cli,
        }
        for provider, target in targets.items():
            if target is None:
                continue
            model = str(models.get(provider, "") or "").strip()
            if model:
                target.model = model

    @staticmethod
    def _sync_modes(config: AppConfig) -> None:
        for target in (
            config.cursor,
            config.openrouter,
            config.openrouter_cli,
            config.opencode_cli,
        ):
            if target is not None:
                target.mode = config.mode

    def save_provider(self, provider: str) -> None:
        self._data["provider"] = provider
        self._write()

    def save_mode(self, mode: str) -> None:
        self._data["mode"] = mode
        self._write()

    def save_model(self, provider: str, model: str) -> None:
        models = self._data.get("models")
        if not isinstance(models, dict):
            models = {}
        models[provider] = model
        self._data["models"] = models
        self._write()
