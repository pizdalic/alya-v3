"""Загрузка libtdjson: путь ALYA_TDJSON_PATH, pip-пакет tdjson или системная библиотека."""

from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import os
from typing import Any, Protocol

from alya.core.errors import AlyaError


class TdJsonUnavailable(AlyaError):
    pass


class TdJson(Protocol):
    def create_client_id(self) -> int: ...

    def send(self, client_id: int, request: str) -> None: ...

    def receive(self, timeout: float) -> str | None: ...

    def execute(self, request: str) -> str | None: ...


def _text(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class CtypesTdJson:
    def __init__(self, path: str) -> None:
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            raise TdJsonUnavailable(f"не удалось загрузить libtdjson ({path}): {exc}") from exc
        lib.td_create_client_id.restype = ctypes.c_int
        lib.td_create_client_id.argtypes = []
        lib.td_send.restype = None
        lib.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
        lib.td_receive.restype = ctypes.c_char_p
        lib.td_receive.argtypes = [ctypes.c_double]
        lib.td_execute.restype = ctypes.c_char_p
        lib.td_execute.argtypes = [ctypes.c_char_p]
        self._lib = lib

    def create_client_id(self) -> int:
        return int(self._lib.td_create_client_id())

    def send(self, client_id: int, request: str) -> None:
        self._lib.td_send(client_id, request.encode("utf-8"))

    def receive(self, timeout: float) -> str | None:
        return _text(self._lib.td_receive(timeout))

    def execute(self, request: str) -> str | None:
        return _text(self._lib.td_execute(request.encode("utf-8")))


class ModuleTdJson:
    """pip-пакет tdjson: функции td_* прямо в модуле."""

    def __init__(self, module: Any) -> None:
        self._m = module

    def create_client_id(self) -> int:
        return int(self._m.td_create_client_id())

    def send(self, client_id: int, request: str) -> None:
        self._m.td_send(client_id, request)

    def receive(self, timeout: float) -> str | None:
        return _text(self._m.td_receive(timeout))

    def execute(self, request: str) -> str | None:
        return _text(self._m.td_execute(request))


def load_tdjson(path: str | None = None) -> TdJson:
    path = path or os.environ.get("ALYA_TDJSON_PATH")
    if path:
        return CtypesTdJson(path)
    try:
        module = importlib.import_module("tdjson")
    except ImportError:
        module = None
    if module is not None and hasattr(module, "td_create_client_id"):
        return ModuleTdJson(module)
    found = ctypes.util.find_library("tdjson")
    if found:
        return CtypesTdJson(found)
    raise TdJsonUnavailable("libtdjson не найдена: установите пакет с extra [tg] "
                            "(pip install 'alya[tg]') или задайте ALYA_TDJSON_PATH")
