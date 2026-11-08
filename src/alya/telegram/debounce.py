"""Debounce: несколько быстрых сообщений одного человека склеиваются в одну реплику."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Bucket[T]:
    items: list[T] = field(default_factory=list)
    started: float = 0.0
    timer: asyncio.TimerHandle | None = None


class Debouncer[K: Hashable, T]:
    def __init__(self, handler: Callable[[K, list[T]], Awaitable[None]], *,
                 delay_s: float = 2.5, max_wait_s: float = 10.0, max_batch: int = 10) -> None:
        self._handler = handler
        self._delay = delay_s
        self._max_wait = max(max_wait_s, delay_s)
        self._max_batch = max_batch
        self._buckets: dict[K, _Bucket[T]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[K, asyncio.Lock] = {}

    def add(self, key: K, item: T) -> None:
        loop = asyncio.get_running_loop()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(started=loop.time())
        bucket.items.append(item)
        if bucket.timer is not None:
            bucket.timer.cancel()
        waited = loop.time() - bucket.started
        if len(bucket.items) >= self._max_batch or waited >= self._max_wait:
            self._flush(key)
            return
        delay = min(self._delay, self._max_wait - waited)
        bucket.timer = loop.call_later(delay, self._flush, key)

    def pending(self) -> int:
        return sum(len(b.items) for b in self._buckets.values())

    def _flush(self, key: K) -> None:
        bucket = self._buckets.pop(key, None)
        if bucket is None or not bucket.items:
            return
        if bucket.timer is not None:
            bucket.timer.cancel()
        task = asyncio.get_running_loop().create_task(self._run(key, bucket.items))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, key: K, items: list[T]) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # пачки одного ключа обрабатываются строго по очереди
            try:
                await self._handler(key, items)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка обработки сообщений")

    async def drain(self) -> None:
        """Немедленно обработать всё накопленное и дождаться обработчиков."""
        for key in list(self._buckets):
            self._flush(key)
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
