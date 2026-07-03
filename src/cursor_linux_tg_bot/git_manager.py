from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_STASH_MSG_RE = re.compile(r"^stash@\{\d+\}$")


@dataclass
class GitCheckpoint:
    head_sha: str
    stash_ref: str | None = None


@dataclass
class GitCommitResult:
    ok: bool
    message: str
    sha: str | None = None


class GitManager:
    def __init__(self, workspace: str, *, enabled: bool = True) -> None:
        self._workspace = workspace
        self._enabled = enabled
        self._repo: bool | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def is_repo(self) -> bool:
        if not self._enabled:
            return False
        if self._repo is not None:
            return self._repo
        code, _, _ = await self._git("rev-parse", "--is-inside-work-tree")
        self._repo = code == 0
        return self._repo

    async def _git(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self._workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode(errors="replace").strip()
        stderr = stderr_b.decode(errors="replace").strip()
        return proc.returncode or 0, stdout, stderr

    async def _is_dirty(self) -> bool:
        code, stdout, _ = await self._git("status", "--porcelain")
        return code == 0 and bool(stdout)

    async def _head_sha(self) -> str:
        code, stdout, stderr = await self._git("rev-parse", "HEAD")
        if code != 0:
            raise RuntimeError(stderr or "git rev-parse HEAD failed")
        return stdout

    def _stash_label(self, chat_id: int) -> str:
        return f"tg-checkpoint-chat{chat_id}"

    async def _find_stash_ref(self, label: str) -> str | None:
        code, stdout, _ = await self._git("stash", "list")
        if code != 0:
            return None
        for line in stdout.splitlines():
            if label in line:
                ref = line.split(":", 1)[0].strip()
                if _STASH_MSG_RE.match(ref):
                    return ref
        return None

    async def create_checkpoint(self, chat_id: int) -> GitCheckpoint | None:
        if not await self.is_repo():
            return None

        head_sha = await self._head_sha()
        stash_ref: str | None = None

        if await self._is_dirty():
            label = self._stash_label(chat_id)
            code, _, stderr = await self._git("stash", "push", "-u", "-m", label)
            if code != 0:
                raise RuntimeError(stderr or "git stash push failed")
            stash_ref = await self._find_stash_ref(label)

        return GitCheckpoint(head_sha=head_sha, stash_ref=stash_ref)

    async def rollback(self, checkpoint: GitCheckpoint) -> str:
        code, _, stderr = await self._git("reset", "--hard", checkpoint.head_sha)
        if code != 0:
            raise RuntimeError(stderr or "git reset --hard failed")

        code, _, stderr = await self._git("clean", "-fd")
        if code != 0:
            raise RuntimeError(stderr or "git clean failed")

        if checkpoint.stash_ref:
            code, _, stderr = await self._git("stash", "apply", checkpoint.stash_ref)
            if code != 0:
                raise RuntimeError(stderr or "git stash apply failed")
            code, _, stderr = await self._git("stash", "drop", checkpoint.stash_ref)
            if code != 0:
                logger.warning("stash drop failed after apply: %s", stderr)

        short = checkpoint.head_sha[:7]
        return f"Откат выполнен к {short}. Изменения последнего сообщения убраны."

    async def has_changes(self) -> bool:
        if not await self.is_repo():
            return False
        code, _, _ = await self._git("diff", "--quiet")
        if code == 1:
            return True
        code, _, _ = await self._git("diff", "--cached", "--quiet")
        if code == 1:
            return True
        code, stdout, _ = await self._git("status", "--porcelain")
        return bool(stdout)

    async def commit(self, message: str) -> GitCommitResult:
        if not await self.is_repo():
            return GitCommitResult(False, "workspace не является git-репозиторием")

        if not await self.has_changes():
            return GitCommitResult(False, "Нет изменений для коммита")

        code, _, stderr = await self._git("add", "-A")
        if code != 0:
            return GitCommitResult(False, stderr or "git add failed")

        code, stdout, stderr = await self._git("commit", "-m", message)
        if code != 0:
            return GitCommitResult(False, stderr or stdout or "git commit failed")

        sha = await self._head_sha()
        return GitCommitResult(True, f"Коммит {sha[:7]}: {message}", sha=sha)

    @staticmethod
    def format_commit_message(
        user_text: str,
        *,
        prefix: str = "tg: ",
        max_length: int = 120,
    ) -> str:
        text = " ".join(user_text.split())
        body = text[: max_length - len(prefix)] if len(prefix) + len(text) > max_length else text
        return f"{prefix}{body}" if body else f"{prefix}update"
