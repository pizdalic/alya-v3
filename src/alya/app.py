"""Сборка приложения: контейнер, компоненты, проверки здоровья."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from alya.core.config import Settings
from alya.core.container import Container
from alya.core.events import Event, EventBus
from alya.core.lifecycle import Lifecycle
from alya.observability.health import Check as HealthCheck
from alya.observability.health import CheckResult, HealthRegistry, HealthServer
from alya.storage.db import Database
from alya.storage.migrator import apply_migrations, load_migrations, pending

log = logging.getLogger(__name__)


class StorageComponent:
    """Открывает БД и применяет миграции при старте."""

    name = "storage"

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self) -> None:
        await asyncio.to_thread(self._db.connect)
        if not self._db.fts5_available():
            log.warning("SQLite собран без FTS5: полнотекстовый поиск будет недоступен")
        applied = await asyncio.to_thread(apply_migrations, self._db.conn)
        if applied:
            log.info("применены миграции: %s", applied)

    async def stop(self) -> None:
        self._db.close()


class EventBusComponent:
    name = "events"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._bus.aclose()


@dataclass(slots=True)
class App:
    settings: Settings
    container: Container
    lifecycle: Lifecycle

    async def run(self) -> None:
        bus = self.container.resolve(EventBus)
        try:
            await self.lifecycle.start()
            await bus.publish(Event("app.started"))
            problems = self.settings.problems_for_run()
            for problem in problems:
                log.warning("конфигурация: %s", problem)
            log.info("основа запущена; Telegram-слой появится в Phase 4")
            await self.lifecycle.run_until_stopped()
        finally:
            await self.lifecycle.stop()
            await self.container.aclose()


def build_app(settings: Settings) -> App:
    container = Container()
    container.register_instance(Settings, settings)
    container.register(EventBus, lambda c: EventBus())
    container.register(
        Database,
        lambda c: Database(settings.storage.db_path, settings.storage.busy_timeout_ms),
    )
    container.register(HealthRegistry, lambda c: HealthRegistry())

    db = container.resolve(Database)
    registry = container.resolve(HealthRegistry)
    registry.add("database", _database_check(db))
    registry.add("migrations", _migrations_check(db))

    lifecycle = Lifecycle()
    lifecycle.add(EventBusComponent(container.resolve(EventBus)))
    lifecycle.add(StorageComponent(db))
    if settings.health.enabled:
        lifecycle.add(HealthServer(registry, settings.health.host, settings.health.port))
    return App(settings=settings, container=container, lifecycle=lifecycle)


def _database_check(db: Database) -> HealthCheck:
    async def check() -> CheckResult:
        rows = await db.fetchall("SELECT 1")
        return CheckResult(ok=rows[0][0] == 1)

    return check


def _migrations_check(db: Database) -> HealthCheck:
    migrations = load_migrations()

    async def check() -> CheckResult:
        left = await asyncio.to_thread(pending, db.conn, migrations)
        if left:
            return CheckResult(False, f"не применены: {[m.version for m in left]}")
        return CheckResult(True, f"версия {migrations[-1].version if migrations else 0}")

    return check

