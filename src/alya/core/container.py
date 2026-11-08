"""Простой DI-контейнер: фабрики-одиночки, защита от циклов, закрытие в обратном порядке."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any, cast

from alya.core.errors import ContainerError

log = logging.getLogger(__name__)


class Container:
    def __init__(self) -> None:
        self._factories: dict[type[Any], Callable[[Container], Any]] = {}
        self._instances: dict[type[Any], Any] = {}
        self._created: list[Any] = []
        self._resolving: list[type[Any]] = []

    def register[T](self, key: type[T], factory: Callable[[Container], T]) -> None:
        if key in self._factories or key in self._instances:
            raise ContainerError(f"зависимость уже зарегистрирована: {key.__name__}")
        self._factories[key] = factory

    def register_instance[T](self, key: type[T], instance: T) -> None:
        if key in self._factories or key in self._instances:
            raise ContainerError(f"зависимость уже зарегистрирована: {key.__name__}")
        # Внешние экземпляры контейнер не закрывает: ими владеет тот, кто их создал.
        self._instances[key] = instance

    def has(self, key: type[Any]) -> bool:
        return key in self._factories or key in self._instances

    def resolve[T](self, key: type[T]) -> T:
        if key in self._instances:
            return cast(T, self._instances[key])
        factory = self._factories.get(key)
        if factory is None:
            raise ContainerError(f"зависимость не зарегистрирована: {key.__name__}")
        if key in self._resolving:
            chain = " -> ".join(k.__name__ for k in [*self._resolving, key])
            raise ContainerError(f"циклическая зависимость: {chain}")
        self._resolving.append(key)
        try:
            instance = factory(self)
        finally:
            self._resolving.pop()
        self._instances[key] = instance
        self._created.append(instance)
        return cast(T, instance)

    async def aclose(self) -> None:
        """Закрыть созданные контейнером объекты в обратном порядке. Ошибки логируются."""
        for instance in reversed(self._created):
            closer = getattr(instance, "aclose", None) or getattr(instance, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("ошибка при закрытии %s", type(instance).__name__)
        created = set(map(id, self._created))
        self._instances = {k: v for k, v in self._instances.items() if id(v) not in created}
        self._created.clear()
