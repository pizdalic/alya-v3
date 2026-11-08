"""Жизненный цикл: компоненты стартуют по порядку и останавливаются в обратном."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Protocol

log = logging.getLogger(__name__)


class Component(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class Lifecycle:
    def __init__(self, stop_timeout_s: float = 10.0) -> None:
        self._components: list[Component] = []
        self._started: list[Component] = []
        self._stop_timeout = stop_timeout_s
        self._stop_requested = asyncio.Event()

    def add(self, component: Component) -> None:
        if self._started:
            raise RuntimeError("нельзя добавлять компоненты после старта")
        self._components.append(component)

    @property
    def started(self) -> list[str]:
        return [c.name for c in self._started]

    async def start(self) -> None:
        for component in self._components:
            try:
                await component.start()
            except BaseException:
                log.error("компонент %s не запустился, откатываю старт", component.name)
                await self.stop()
                raise
            self._started.append(component)
            log.info("компонент запущен: %s", component.name)

    async def stop(self) -> None:
        while self._started:
            component = self._started.pop()
            try:
                await asyncio.wait_for(component.stop(), timeout=self._stop_timeout)
                log.info("компонент остановлен: %s", component.name)
            except TimeoutError:
                log.error("компонент %s не остановился за %ss", component.name, self._stop_timeout)
            except Exception:
                log.exception("ошибка остановки %s", component.name)

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def run_until_stopped(self, install_signals: bool = True) -> None:
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, self.request_stop)
        try:
            await self.start()
            await self._stop_requested.wait()
            log.info("получен запрос на остановку")
        finally:
            await self.stop()
            if install_signals:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    with contextlib.suppress(NotImplementedError, RuntimeError):
                        loop.remove_signal_handler(sig)
