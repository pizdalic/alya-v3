"""Расписание Dream: ночью по местному времени и при накоплении новых записей.

Работает в фоне и не блокирует ответы: каждый шаг движка — короткая транзакция.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from alya.core.timeutil import Clock, parse_iso, utcnow
from alya.dream.engine import DreamEngine, DreamReport

log = logging.getLogger(__name__)


class DreamScheduler:
    name = "dream"

    def __init__(
        self, engine: DreamEngine, *, timezone: str, hour_local: int = 4,
        check_interval_s: float = 600.0, new_memories_threshold: int = 200,
        min_gap_hours: float = 6.0, clock: Clock = utcnow,
    ) -> None:
        self._engine = engine
        self._tz = ZoneInfo(timezone)
        self._hour = hour_local
        self._interval = check_interval_s
        self._threshold = new_memories_threshold
        self._min_gap = timedelta(hours=min_gap_hours)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def due(self) -> str | None:
        """Причина запуска (schedule / volume) или None, если рано."""
        last = await self._engine.last_run()
        now = self._clock()
        last_start = parse_iso(last["started_at"]) if last else None
        if last_start is not None and now - last_start < self._min_gap:
            return None
        local = now.astimezone(self._tz)
        if local.hour >= self._hour and (
                last_start is None or last_start.astimezone(self._tz).date() < local.date()):
            return "schedule"
        since = last["started_at"] if last else None
        if await self._engine.new_memories_since(since) >= self._threshold:
            return "volume"
        return None

    async def run_now(self, reason: str = "manual") -> DreamReport | None:
        if self._lock.locked():
            return None
        async with self._lock:
            return await self._engine.run(reason)

    async def tick(self) -> DreamReport | None:
        reason = await self.due()
        if reason is None:
            return None
        return await self.run_now(reason)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dream: прогон завершился ошибкой")
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        interrupted = await self._engine.recover()
        if interrupted:
            log.warning("dream: прерванных прогонов после рестарта: %d", interrupted)
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
