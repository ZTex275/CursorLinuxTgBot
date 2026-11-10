from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

from .agent_base import RunUpdate
from .config import OpenCodeCliConfig
from .git_manager import GitCheckpoint
from .platform import is_binary_available, is_windows, resolve_binary
from .session_store import ChatKey, ChatSession, SessionStore

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_BOX_LINE_RE = re.compile(r"^[\s│|┃]+(.*?)[\s│|┃]+$")


def _strip_ansi(text: str) -> str:
    return _OSC_RE.sub("", _ANSI_RE.sub("", text))


def _clean_opencode_output(raw: str) -> str:
    text = _strip_ansi(raw).replace("\r\n", "\n")
    lines: list[str] = []
    for raw_line in text.split("\n"):
        # `opencode run` may redraw a status line with carriage returns.
        line = raw_line.split("\r")[-1].strip()
        if not line:
            continue
        if line.startswith(("┌", "└", "├", "╭", "╰", "╞", "╡")):
            continue
        if all(ch in "─━═-_│|┃ " for ch in line):
            continue
        match = _BOX_LINE_RE.match(line)
        if match:
            content = match.group(1).strip()
            if content:
                lines.append(content)
            continue
        lines.append(line)
    return "\n".join(lines).strip()


class OpenCodeCliSessionManager:
    def __init__(
        self,
        config: OpenCodeCliConfig,
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
        self._binary = resolve_binary(config.binary)
        self._active_procs: dict[ChatKey, asyncio.subprocess.Process] = {}
        self._cancelled: set[ChatKey] = set()

    async def start(self) -> None:
        if not is_binary_available(self._binary):
            raise RuntimeError(
                f"OpenCode CLI ({self._binary}) не найден. "
                "Установите: npm install -g @opencode/cli "
                "(или curl -fsSL https://opencode.ai/v2/install | bash), "
                "затем выполните opencode auth login."
            )

    async def stop(self) -> None:
        return

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
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
            logger.exception("cancel opencode process failed chat_id=%s", chat_id)
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

    def _run_command(self, prompt: str, *, mode: str) -> list[str]:
        cmd = [self._binary, "run"]

        if self._config.standalone:
            cmd.append("--standalone")
        if self._config.auto_approve:
            cmd.append("--auto")
        if self._config.archived:
            cmd.append("--archived")
        if self._config.model:
            cmd.extend(["--model", self._config.model])

        agent = self._config.plan_agent if mode == "plan" else self._config.agent
        if agent:
            cmd.extend(["--agent", agent])

        cmd.extend(self._config.extra_args)
        cmd.append(prompt)
        if is_windows() and self._binary.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", *cmd]
        return cmd

    async def _run_opencode(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str,
    ) -> AsyncIterator[RunUpdate | str]:
        cmd = self._run_command(prompt, mode=mode)
        logger.debug("opencode command: %s", " ".join(cmd[:4] + ["…"] if len(cmd) > 4 else cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._workspace,
            env=self._subprocess_env(),
        )
        self._active_procs[chat_id] = proc

        timeout = max(self._config.timeout_sec, 30.0)
        raw_buffer = ""
        last_emit = 0.0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        assert proc.stdout is not None

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    yield RunUpdate(
                        text="",
                        done=True,
                        error=f"OpenCode CLI: таймаут {timeout:.0f}с без вывода",
                    )
                    return

                if not chunk:
                    raw_buffer += decoder.decode(b"", final=True)
                    break

                raw_buffer += decoder.decode(chunk)
                cleaned = _clean_opencode_output(raw_buffer)
                now = time.monotonic()
                if cleaned and now - last_emit >= 0.5:
                    last_emit = now
                    yield RunUpdate(text=cleaned, done=False, stage="Генерирую ответ")

            code = await proc.wait()
        except Exception as err:
            proc.kill()
            await proc.wait()
            yield RunUpdate(text="", done=True, error=f"OpenCode CLI: {err}")
            return
        finally:
            was_cancelled = chat_id in self._cancelled
            self._active_procs.pop(chat_id, None)
            self._cancelled.discard(chat_id)

        cleaned = _clean_opencode_output(raw_buffer)
        if was_cancelled:
            yield RunUpdate(text=cleaned or "Остановлено.", done=True, cancelled=True)
            return

        if code != 0 and not cleaned:
            yield RunUpdate(
                text="",
                done=True,
                error=f"OpenCode CLI завершился с кодом {code}: {_strip_ansi(raw_buffer)[:500]}",
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

        yield RunUpdate(text="", done=False, stage="Генерирую ответ")
        async for item in self._run_opencode(chat_id, full_prompt, mode=effective_mode):
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
