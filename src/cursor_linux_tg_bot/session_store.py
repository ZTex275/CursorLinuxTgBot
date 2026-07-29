from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .git_manager import GitCheckpoint


@dataclass
class ChatSession:
    agent_id: str | None = None
    messages: list[dict] | None = None
    git_checkpoint: GitCheckpoint | None = None
    last_user_message: str | None = None
    turn_count: int = 0
    last_input_tokens: int = 0
    context_summary: str | None = None

    def to_dict(self) -> dict:
        data: dict = {}
        if self.agent_id:
            data["agent_id"] = self.agent_id
        if self.messages:
            data["messages"] = self.messages
        if self.git_checkpoint:
            data["git_checkpoint"] = asdict(self.git_checkpoint)
        if self.last_user_message:
            data["last_user_message"] = self.last_user_message
        if self.turn_count:
            data["turn_count"] = self.turn_count
        if self.last_input_tokens:
            data["last_input_tokens"] = self.last_input_tokens
        if self.context_summary:
            data["context_summary"] = self.context_summary
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ChatSession:
        cp = data.get("git_checkpoint")
        checkpoint = GitCheckpoint(**cp) if cp else None
        raw_messages = data.get("messages")
        messages = list(raw_messages) if raw_messages else None
        return cls(
            agent_id=data.get("agent_id"),
            messages=messages,
            git_checkpoint=checkpoint,
            last_user_message=data.get("last_user_message"),
            turn_count=int(data.get("turn_count") or 0),
            last_input_tokens=int(data.get("last_input_tokens") or 0),
            context_summary=data.get("context_summary"),
        )


ChatKey = int | str


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: ChatKey) -> Path:
        return self._dir / f"{chat_id}.json"

    def load(self, chat_id: ChatKey) -> ChatSession:
        path = self._path(chat_id)
        if not path.exists():
            return ChatSession()
        return ChatSession.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, chat_id: ChatKey, session: ChatSession) -> None:
        data = session.to_dict()
        path = self._path(chat_id)
        if not data:
            if path.exists():
                path.unlink()
            return
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self, chat_id: ChatKey) -> None:
        path = self._path(chat_id)
        if path.exists():
            path.unlink()
