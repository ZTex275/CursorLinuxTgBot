from __future__ import annotations

from .agent_base import AgentSessionManager
from .agent_factory import create_session_manager
from .config import AppConfig, provider_help_text, provider_status_text, validate_provider_choice
from .message_queue import MessageQueue


def any_active_tasks(sessions: AgentSessionManager) -> bool:
    locks = getattr(sessions, "_locks", None)
    if not locks:
        return False
    return any(lock.locked() for lock in locks.values())


def any_pending_queues(*queues: MessageQueue) -> bool:
    return any(queue.total_pending() > 0 for queue in queues)


async def switch_provider(
    config: AppConfig,
    sessions: AgentSessionManager,
    new_provider: str | None,
    *,
    queues: tuple[MessageQueue, ...] = (),
) -> tuple[AgentSessionManager | None, str]:
    if not new_provider:
        return None, provider_status_text(config)

    error = validate_provider_choice(config, new_provider)
    if error:
        return None, error

    if config.provider == new_provider:
        return sessions, f"Провайдер уже {config.provider_label}."

    if any_active_tasks(sessions) or any_pending_queues(*queues):
        return None, "Сначала дождитесь завершения задач, очистите очередь (/stop) и повторите."

    await sessions.stop()

    config.provider = new_provider
    if config.cursor is not None:
        config.cursor.mode = config.mode
    if config.openrouter is not None:
        config.openrouter.mode = config.mode
    if config.openrouter_cli is not None:
        config.openrouter_cli.mode = config.mode

    new_sessions = create_session_manager(config)
    await new_sessions.start()
    return new_sessions, f"Провайдер: {config.provider_label}\nmodel: {config.model}"
