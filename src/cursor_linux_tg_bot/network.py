from __future__ import annotations

import socket
from typing import Any

import httpx


def enable_ipv4_only() -> None:
    """Не использовать IPv6 (на Orange Pi AAAA-записи часто недоступны)."""
    original = socket.getaddrinfo

    def getaddrinfo_ipv4(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4  # type: ignore[assignment]


def telegram_transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(local_address="0.0.0.0")
