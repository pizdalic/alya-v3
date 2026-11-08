"""Асинхронный Event Bus.

Ошибка или зависание одного подписчика не ломает остальных. В лог пишется только
имя события и обработчика — не payload (там могут быть личные данные).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


type Handler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PublishResult:
    delivered: int
    failed: int


class EventBus:
    def __init__(self, handler_timeout_s: float = 30.0) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._pending: set[asyncio.Task[PublishResult]] = set()
        self._timeout = handler_timeout_s
        self._closed = False

    def subscribe(self, name: str, handler: Handler) -> Callable[[], None]:
        """Подписаться; name="*" — на все события. Возвращает функцию отписки."""
        self._handlers[name].append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(name, [])
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> PublishResult:
        """Доставить событие всем подписчикам параллельно и дождаться всех."""
        self._ensure_open()
        return await self._deliver(event)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("EventBus закрыт")

    async def _deliver(self, event: Event) -> PublishResult:
        handlers = [*self._handlers.get(event.name, []), *self._handlers.get(WILDCARD, [])]
        if not handlers:
            return PublishResult(delivered=0, failed=0)
        results = await asyncio.gather(*(self._run(h, event) for h in handlers))
        failed = results.count(False)
        return PublishResult(delivered=len(results) - failed, failed=failed)

    def publish_nowait(self, event: Event) -> None:
        """Опубликовать в фоне. Задача хранится до завершения, иначе GC может её убить."""
        self._ensure_open()
        task = asyncio.get_running_loop().create_task(self._deliver(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def aclose(self) -> None:
        self._closed = True
        await self.drain()

    async def _run(self, handler: Handler, event: Event) -> bool:
        name = getattr(handler, "__qualname__", repr(handler))
        try:
            await asyncio.wait_for(handler(event), timeout=self._timeout)
            return True
        except TimeoutError:
            log.error("обработчик %s не уложился в таймаут на %s", name, event.name)
        except Exception:
            log.exception("обработчик %s упал на %s", name, event.name)
        return False
