"""Broadcaster assíncrono thread-safe para distribuição de eventos SSE."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SSEMessage:
    """Mensagem estruturada no formato Server-Sent Events."""

    event: str
    data: dict[str, Any]

    def encode(self) -> str:
        serialized = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.event}\ndata: {serialized}\n\n"


class SSEBroadcaster:
    """Gerencia inscrições SSE com filas limitadas e proteção contra backpressure."""

    def __init__(self, max_queue_size: int = 32) -> None:
        self._max_queue_size = max_queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[SSEMessage | None]] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[SSEMessage | None]:
        queue: asyncio.Queue[SSEMessage | None] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SSEMessage | None]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """Publica evento SSE com segurança tanto do event loop quanto de threads externas."""
        message = SSEMessage(event=event, data=data)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            self._enqueue_all(message)
        else:
            loop.call_soon_threadsafe(self._enqueue_all, message)

    def close_all(self) -> None:
        """Envia sentinela None para encerrar suavemente todos os clientes SSE conectados."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._subscribers.clear()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            self._enqueue_all(None)
        else:
            loop.call_soon_threadsafe(self._enqueue_all, None)

    def _enqueue_all(self, item: SSEMessage | None) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)
