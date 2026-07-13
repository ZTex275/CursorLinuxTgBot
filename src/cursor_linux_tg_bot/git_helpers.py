from __future__ import annotations

from .config import GitConfig
from .git_manager import GitCommitResult, GitManager


async def format_commit_reply(
    git: GitManager,
    result: GitCommitResult,
    git_cfg: GitConfig,
) -> str:
    if not result.ok:
        return f"ℹ️ {result.message}"
    lines = [f"✅ {result.message}"]
    if git_cfg.auto_push:
        push = await git.push()
        lines.append(f"⬆️ {push.message}" if push.ok else f"⚠️ push: {push.message}")
    return "\n".join(lines)


async def append_push_note(git: GitManager, note: str, git_cfg: GitConfig) -> str:
    if not git_cfg.auto_push:
        return note
    push = await git.push()
    if push.ok:
        return f"{note}; push: {push.message}"
    return f"{note}; push failed: {push.message}"
