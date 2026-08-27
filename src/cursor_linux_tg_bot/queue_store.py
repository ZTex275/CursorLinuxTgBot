from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ChatKey = int | str


@dataclass
class StoredQueueItem:
    id: str
    queue: str
    chat_id: ChatKey
    user_text: str
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "queue": self.queue,
            "chat_id": self.chat_id,
            "user_text": self.user_text,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredQueueItem:
        return cls(
            id=str(data["id"]),
            queue=str(data["queue"]),
            chat_id=data["chat_id"],
            user_text=str(data["user_text"]),
            meta=dict(data.get("meta") or {}),
        )


class QueueStore:
    """Persistent FIFO queue items survive bot restarts."""

    def __init__(self, queue_dir: Path) -> None:
        self._path = queue_dir / "pending.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_items(self) -> list[StoredQueueItem]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [StoredQueueItem.from_dict(item) for item in items if isinstance(item, dict)]

    def _write_items(self, items: list[StoredQueueItem]) -> None:
        payload = {"items": [item.to_dict() for item in items]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def list_queue(self, queue_name: str) -> list[StoredQueueItem]:
        return [item for item in self._read_items() if item.queue == queue_name]

    def add(
        self,
        queue_name: str,
        chat_id: ChatKey,
        user_text: str,
        meta: dict[str, Any],
    ) -> str:
        item_id = uuid.uuid4().hex
        items = self._read_items()
        items.append(
            StoredQueueItem(
                id=item_id,
                queue=queue_name,
                chat_id=chat_id,
                user_text=user_text,
                meta=meta,
            )
        )
        self._write_items(items)
        return item_id

    def remove(self, item_id: str) -> None:
        items = self._read_items()
        filtered = [item for item in items if item.id != item_id]
        if len(filtered) != len(items):
            self._write_items(filtered)

    def clear_chat(self, queue_name: str, chat_id: ChatKey) -> None:
        items = self._read_items()
        filtered = [
            item for item in items if not (item.queue == queue_name and item.chat_id == chat_id)
        ]
        if len(filtered) != len(items):
            self._write_items(filtered)
