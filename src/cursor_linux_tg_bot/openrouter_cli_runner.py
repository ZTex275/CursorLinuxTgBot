from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import AsyncIterator

from .agent_base import RunUpdate
from .config import OpenRouterCliConfig
from .git_manager import GitCheckpoint
from .session_store import ChatKey, ChatSession, SessionStore

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_LINE_RE = re.compile(r"^[\s│|]+(.*)[\s│|]+$")
_ASK_TIMEOUT_SEC = 600.0


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _clean_orc_output(raw: str) -> str:
    text = _strip_ansi(raw)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("┌", "└", "├", "╭", "╰", "╞", "╡")):
            continue
        if stripped and all(ch in "─━═-_ " for ch in stripped):
            continue
        match = _BOX_LINE_RE.match(stripped)
        if match:
            content = match.group(1).strip()
            if content:
                lines.append(content)
            continue
        if stripped.startswith("Rate limited.") or stripped.startswith("Request failed."):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


class OpenRouterCliSessionManager:
    def __init__(
        self,
        config: OpenRouterCliConfig,
        *,
        workspace: str,
        sessions_dir: Path,
        system_prefix: str,
    ) -> None:
        self._config = config
        self._workspace = Path(workspace).resolve()
        self._sessions = SessionStore(sessions_dir)
        self._system_prefix = system_prefix.strip()
        self._locks: dict[ChatKey, asyncio.Lock] = {}
        self._binary = self._resolve_binary(config.binary)
        self._active_procs: dict[ChatKey, asyncio.subprocess.Process] = {}
        self._cancelled: set[ChatKey] = set()

    def _resolve_binary(self, configured: str) -> str:
        candidate = configured.strip() or "orc"
        if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
        home = Path.home()
        for path in (
            home / ".npm-global" / "bin" / candidate,
            home / ".local" / "bin" / candidate,
            Path("/usr/local/bin") / candidate,
        ):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return candidate

    async def start(self) -> None:
        if not shutil.which(self._binary) and not os.access(self._binary, os.X_OK):
            raise RuntimeError(
                f"OpenRouter CLI ({self._binary}) не найден. "
                "Установите: npm install -g openrouter-cli"
            )
        await self._ensure_api_key()

    async def stop(self) -> None:
        return

    async def _ensure_api_key(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            "config",
            "set-key",
            self._config.api_key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(f"Не удалось задать API key для orc: {err or proc.returncode}")

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["CI"] = "1"
        return env

    def lock_for(self, chat_id: ChatKey) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def load_session(self, chat_id: ChatKey) -> ChatSession:
        return self._sessions.load(chat_id)

    def save_session(self, chat_id: ChatKey, session: ChatSession) -> None:
        self._sessions.save(chat_id, session)

    def set_git_checkpoint(
        self,
        chat_id: ChatKey,
        checkpoint: GitCheckpoint | None,
        user_message: str | None = None,
    ) -> None:
        session = self._sessions.load(chat_id)
        session.git_checkpoint = checkpoint
        if user_message is not None:
            session.last_user_message = user_message
        self._sessions.save(chat_id, session)

    def clear_git_checkpoint(self, chat_id: ChatKey) -> None:
        session = self._sessions.load(chat_id)
        session.git_checkpoint = None
        self._sessions.save(chat_id, session)

    async def reset_chat(self, chat_id: ChatKey) -> None:
        self._sessions.clear(chat_id)

    async def cancel_active(self, chat_id: ChatKey) -> bool:
        proc = self._active_procs.get(chat_id)
        if proc is None:
            return False
        self._cancelled.add(chat_id)
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("cancel orc process failed chat_id=%s", chat_id)
        return True

    def _system_message(self, *, mode: str) -> dict[str, str]:
        plan_note = (
            "\nРежим plan: только план и советы, без выполнения команд на сервере."
            if mode == "plan"
            else ""
        )
        return {
            "role": "system",
            "content": (
                f"{self._system_prefix}\n\n"
                f"Рабочая директория (workspace): {self._workspace}{plan_note}"
            ),
        }

    def _build_prompt(self, messages: list[dict[str, str]], prompt: str) -> str:
        parts: list[str] = []
        for message in messages:
            role = message.get("role", "")
            content = (message.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                parts.append(f"[system]\n{content}")
            elif role == "user":
                parts.append(f"[user]\n{content}")
            elif role == "assistant":
                parts.append(f"[assistant]\n{content}")
        parts.append(f"[user]\n{prompt}")
        return "\n\n".join(parts)

    def _ask_command(self, prompt: str) -> list[str]:
        cmd = [self._binary, "ask", prompt]
        if self._config.model:
            cmd.extend(["--model", self._config.model])
        elif self._config.profile:
            cmd.extend(["--profile", self._config.profile])
        cmd.extend(self._config.extra_args)
        return cmd

    async def _run_orc_ask(self, chat_id: ChatKey, prompt: str) -> AsyncIterator[RunUpdate | str]:
        cmd = self._ask_command(prompt)
        logger.debug("orc command: %s", " ".join(cmd[:3] + ["…"] if len(cmd) > 3 else cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._workspace,
            env=self._subprocess_env(),
        )
        self._active_procs[chat_id] = proc

        raw_buffer = ""
        last_emit = 0.0
        assert proc.stdout is not None

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=_ASK_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    yield RunUpdate(text="", done=True, error=f"OpenRouter CLI: таймаут {_ASK_TIMEOUT_SEC:.0f}с")
                    return

                if not chunk:
                    break

                raw_buffer += chunk.decode(errors="replace")
                cleaned = _clean_orc_output(raw_buffer)
                now = time.monotonic()
                if cleaned and now - last_emit >= 0.5:
                    last_emit = now
                    yield RunUpdate(text=cleaned, done=False)

            code = await proc.wait()
        except Exception as err:
            proc.kill()
            await proc.wait()
            yield RunUpdate(text="", done=True, error=f"OpenRouter CLI: {err}")
            return
        finally:
            was_cancelled = chat_id in self._cancelled
            self._active_procs.pop(chat_id, None)
            self._cancelled.discard(chat_id)

        cleaned = _clean_orc_output(raw_buffer)
        if was_cancelled:
            yield RunUpdate(text=cleaned.strip() or "Остановлено.", done=True, cancelled=True)
            return

        if code != 0 and not cleaned:
            yield RunUpdate(
                text="",
                done=True,
                error=f"OpenRouter CLI завершился с кодом {code}: {_strip_ansi(raw_buffer)[:500]}",
            )
            return

        yield cleaned or "Готово."

    async def run_prompt(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[RunUpdate]:
        effective_mode = mode or self._config.mode
        session = self._sessions.load(chat_id)
        messages = list(session.messages or [])
        if not messages:
            messages = [self._system_message(mode=effective_mode)]
        messages.append({"role": "user", "content": prompt})

        full_prompt = self._build_prompt(messages[:-1], prompt)
        buffer = ""
        final_text = ""

        async for item in self._run_orc_ask(chat_id, full_prompt):
            if isinstance(item, RunUpdate):
                if item.error or item.cancelled:
                    yield item
                    return
                buffer = item.text
                yield item
            else:
                final_text = item

        final_text = (final_text or buffer).strip() or "Готово."
        messages.append({"role": "assistant", "content": final_text})
        session.messages = messages
        self._sessions.save(chat_id, session)
        yield RunUpdate(text=final_text, done=True)
