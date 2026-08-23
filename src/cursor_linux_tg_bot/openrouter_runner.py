from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .agent_base import RunUpdate
from .config import OpenRouterConfig
from .session_store import ChatKey, ChatSession, SessionStore
from .textutil import stage_from_tool_call

logger = logging.getLogger(__name__)

_SHELL_TIMEOUT_SEC = 120.0
_MAX_TOOL_OUTPUT = 30_000


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Выполнить shell-команду в рабочей директории workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Команда для bash -lc"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать текстовый файл относительно workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать текст в файл относительно workspace (создаёт каталоги при необходимости).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу"},
                    "content": {"type": "string", "description": "Содержимое файла"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Список файлов и каталогов в директории workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Относительный путь (пустая строка = корень workspace)",
                        "default": "",
                    },
                },
            },
        },
    },
]


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: str = ""


@dataclass
class _StreamResult:
    content: str = ""
    tool_calls: list[_ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    error: str | None = None


class OpenRouterSessionManager:
    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        workspace: str,
        sessions_dir: Path,
        system_prefix: str,
    ) -> None:
        self._config = config
        self._workspace = Path(workspace).resolve()
        self._sessions = SessionStore(sessions_dir)
        self._system_prefix = system_prefix.strip()
        self._client: httpx.AsyncClient | None = None
        self._locks: dict[ChatKey, asyncio.Lock] = {}
        self._active_chats: set[ChatKey] = set()
        self._cancelled: set[ChatKey] = set()
        self._active_procs: dict[ChatKey, list[asyncio.subprocess.Process]] = {}

    def _system_message(self) -> dict[str, str]:
        return {
            "role": "system",
            "content": (
                f"{self._system_prefix}\n\n"
                f"Рабочая директория (workspace): {self._workspace}\n"
                "Используй инструменты shell, read_file, write_file, list_dir для выполнения задач на сервере."
            ),
        }

    async def start(self) -> None:
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
        checkpoint,
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
        if chat_id not in self._active_chats:
            return False
        self._cancelled.add(chat_id)
        for proc in self._active_procs.pop(chat_id, []):
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception("cancel shell process failed chat_id=%s", chat_id)
        return True

    def _is_cancelled(self, chat_id: ChatKey) -> bool:
        return chat_id in self._cancelled

    def _resolve_path(self, rel_path: str) -> Path:
        rel = (rel_path or ".").strip() or "."
        target = (self._workspace / rel).resolve()
        if self._workspace not in target.parents and target != self._workspace:
            raise ValueError(f"Путь вне workspace: {rel_path}")
        return target

    def _truncate(self, text: str) -> str:
        if len(text) <= _MAX_TOOL_OUTPUT:
            return text
        return text[: _MAX_TOOL_OUTPUT - 20] + "\n… [обрезано]"

    async def _run_shell(self, chat_id: ChatKey, command: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self._workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active_procs.setdefault(chat_id, []).append(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SHELL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Ошибка: таймаут {_SHELL_TIMEOUT_SEC:.0f}с"
        finally:
            procs = self._active_procs.get(chat_id, [])
            if proc in procs:
                procs.remove(proc)
        out = (stdout or b"").decode(errors="replace")
        err = (stderr or b"").decode(errors="replace")
        code = proc.returncode or 0
        parts = [f"exit_code={code}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return self._truncate("\n".join(parts))

    async def _read_file(self, rel_path: str) -> str:
        path = self._resolve_path(rel_path)
        if not path.is_file():
            return f"Ошибка: файл не найден: {rel_path}"
        try:
            return self._truncate(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as err:
            return f"Ошибка чтения: {err}"

    async def _write_file(self, rel_path: str, content: str) -> str:
        path = self._resolve_path(rel_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Записано {len(content)} символов в {rel_path}"
        except OSError as err:
            return f"Ошибка записи: {err}"

    async def _list_dir(self, rel_path: str) -> str:
        path = self._resolve_path(rel_path or ".")
        if not path.exists():
            return f"Ошибка: путь не найден: {rel_path or '.'}"
        if not path.is_dir():
            return f"Ошибка: не каталог: {rel_path}"
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for entry in entries[:500]:
            kind = "dir" if entry.is_dir() else "file"
            lines.append(f"[{kind}] {entry.name}")
        if len(entries) > 500:
            lines.append(f"… ещё {len(entries) - 500} элементов")
        return "\n".join(lines) or "(пусто)"

    async def _execute_tool(self, chat_id: ChatKey, call: _ToolCall) -> str:
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as err:
            return f"Ошибка JSON аргументов: {err}"

        name = call.name
        try:
            if name == "shell":
                return await self._run_shell(chat_id, str(args.get("command", "")))
            if name == "read_file":
                return await self._read_file(str(args.get("path", "")))
            if name == "write_file":
                return await self._write_file(str(args.get("path", "")), str(args.get("content", "")))
            if name == "list_dir":
                return await self._list_dir(str(args.get("path", "")))
            return f"Неизвестный инструмент: {name}"
        except Exception as err:
            logger.exception("tool %s failed", name)
            return f"Ошибка инструмента {name}: {err}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        if self._config.site_url:
            headers["HTTP-Referer"] = self._config.site_url
        if self._config.app_name:
            headers["X-Title"] = self._config.app_name
        return headers

    async def _stream_completion(
        self,
        chat_id: ChatKey,
        messages: list[dict[str, Any]],
        *,
        use_tools: bool,
    ) -> AsyncIterator[RunUpdate | _StreamResult]:
        assert self._client is not None
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": True,
        }
        if use_tools:
            payload["tools"] = TOOLS

        buffer = ""
        tool_calls: dict[int, _ToolCall] = {}
        finish_reason: str | None = None

        try:
            async with self._client.stream(
                "POST",
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    text = body.decode(errors="replace")
                    yield _StreamResult(error=f"OpenRouter HTTP {response.status_code}: {text[:500]}")
                    return

                last_emit = 0.0
                async for line in response.aiter_lines():
                    if self._is_cancelled(chat_id):
                        return
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if "error" in chunk:
                        err = chunk["error"]
                        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        yield _StreamResult(error=f"OpenRouter: {message}")
                        return

                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        finish_reason = choice.get("finish_reason") or finish_reason

                        content = delta.get("content")
                        if content:
                            buffer += content
                            now = time.monotonic()
                            if now - last_emit >= 0.5:
                                last_emit = now
                                yield RunUpdate(text=buffer, done=False, stage="Генерирую ответ")

                        for raw_call in delta.get("tool_calls") or []:
                            index = int(raw_call.get("index", 0))
                            if index not in tool_calls:
                                tool_calls[index] = _ToolCall(
                                    id=raw_call.get("id") or f"call_{index}",
                                    name=(raw_call.get("function") or {}).get("name") or "",
                                )
                            call = tool_calls[index]
                            fn = raw_call.get("function") or {}
                            if fn.get("name"):
                                call.name = fn["name"]
                            if fn.get("arguments"):
                                call.arguments += fn["arguments"]

        except httpx.HTTPError as err:
            yield _StreamResult(error=f"OpenRouter: {err}")
            return

        ordered_calls = [tool_calls[i] for i in sorted(tool_calls)]
        yield _StreamResult(content=buffer, tool_calls=ordered_calls, finish_reason=finish_reason)

    def _append_assistant_tool_message(self, messages: list[dict[str, Any]], result: _StreamResult) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": result.content or None}
        if result.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments or "{}"},
                }
                for call in result.tool_calls
            ]
        messages.append(message)

    async def run_prompt(
        self,
        chat_id: ChatKey,
        prompt: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[RunUpdate]:
        effective_mode = mode or self._config.mode
        use_tools = effective_mode == "agent"

        session = self._sessions.load(chat_id)
        messages = list(session.messages or [])
        if not messages:
            messages = [self._system_message()]
        messages.append({"role": "user", "content": prompt})

        buffer = ""
        self._active_chats.add(chat_id)
        self._cancelled.discard(chat_id)
        try:
            for _ in range(self._config.max_tool_rounds):
                if self._is_cancelled(chat_id):
                    yield RunUpdate(text=buffer.strip() or "Остановлено.", done=True, cancelled=True)
                    return

                final_result: _StreamResult | None = None
                async for item in self._stream_completion(chat_id, messages, use_tools=use_tools):
                    if self._is_cancelled(chat_id):
                        yield RunUpdate(text=buffer.strip() or "Остановлено.", done=True, cancelled=True)
                        return
                    if isinstance(item, RunUpdate):
                        buffer = item.text
                        yield item
                    else:
                        final_result = item

                if self._is_cancelled(chat_id):
                    yield RunUpdate(text=buffer.strip() or "Остановлено.", done=True, cancelled=True)
                    return

                if final_result is None:
                    yield RunUpdate(text="", done=True, error="OpenRouter: пустой ответ")
                    return
                if final_result.error:
                    yield RunUpdate(text=buffer, done=True, error=final_result.error)
                    return

                buffer = final_result.content
                if not final_result.tool_calls:
                    messages.append({"role": "assistant", "content": final_result.content or buffer})
                    session.messages = messages
                    self._sessions.save(chat_id, session)
                    final_text = (final_result.content or buffer).strip() or "Готово."
                    yield RunUpdate(text=final_text, done=True)
                    return

                if not use_tools:
                    final_text = (final_result.content or buffer).strip() or "Готово."
                    yield RunUpdate(text=final_text, done=True)
                    return

                self._append_assistant_tool_message(messages, final_result)
                for call in final_result.tool_calls:
                    if self._is_cancelled(chat_id):
                        yield RunUpdate(text=buffer.strip() or "Остановлено.", done=True, cancelled=True)
                        return
                    stage, detail = stage_from_tool_call(call.name, call.arguments)
                    yield RunUpdate(
                        text=buffer,
                        done=False,
                        stage=stage,
                        detail=detail,
                    )
                    result = await self._execute_tool(chat_id, call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result,
                        }
                    )

            yield RunUpdate(
                text=buffer,
                done=True,
                error=f"Превышен лимит шагов инструментов ({self._config.max_tool_rounds}).",
            )
            session.messages = messages
            self._sessions.save(chat_id, session)
        finally:
            self._active_chats.discard(chat_id)
            self._cancelled.discard(chat_id)
            self._active_procs.pop(chat_id, None)
