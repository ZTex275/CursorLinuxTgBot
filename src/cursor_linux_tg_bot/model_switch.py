from __future__ import annotations

from .agent_base import AgentSessionManager
from .config import AppConfig, apply_model, model_status_text
from .message_queue import MessageQueue
from .provider_switch import any_active_tasks, any_pending_queues


async def switch_model(
    config: AppConfig,
    sessions: AgentSessionManager,
    new_model: str | None,
    *,
    queues: tuple[MessageQueue, ...] = (),
) -> str:
    if not new_model:
        return model_status_text(config)

    if any_active_tasks(sessions) or any_pending_queues(*queues):
        return "Сначала дождитесь завершения задач, очистите очередь (/stop) и повторите."

    error = apply_model(config, new_model)
    if error:
        return error

    invalidate = getattr(sessions, "invalidate_for_model_change", None)
    if invalidate is not None:
        await invalidate()

    return f"Модель ({config.provider_label}): {config.model}"
