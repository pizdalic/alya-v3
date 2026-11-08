"""Асинхронный клиент TDLib.

td_receive читается в отдельном потоке; ответы сопоставляются с запросами по @extra,
обновления разбираются строго по очереди одной задачей.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from alya.telegram.tdlib.tdjson import TdJson
from alya.telegram.transport import FloodWait, TransportError

log = logging.getLogger(__name__)

type TdObject = dict[str, Any]
type UpdateHandler = Callable[[TdObject], Awaitable[None]]

_RETRY_AFTER = re.compile(r"retry after (\d+)", re.I)
_PERMANENT = frozenset({400, 403, 404, 406})


class TdError(TransportError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"TDLib {code}: {message}", permanent=code in _PERMANENT)
        self.code = code
        self.td_message = message


def raise_for_error(resp: TdObject) -> TdObject:
    if resp.get("@type") != "error":
        return resp
    code = int(resp.get("code") or 0)
    message = str(resp.get("message") or "")
    if code == 429:
        match = _RETRY_AFTER.search(message)
        raise FloodWait(float(match.group(1)) if match else 30.0)
    raise TdError(code, message)


class TdClient:
    def __init__(self, td: TdJson, *, receive_timeout: float = 1.0,
                 request_timeout: float = 60.0) -> None:
        self._td = td
        self._receive_timeout = receive_timeout
        self._request_timeout = request_timeout
        self.client_id: int | None = None
        self._pending: dict[str, asyncio.Future[TdObject]] = {}
        self._handlers: list[UpdateHandler] = []
        self._queue: asyncio.Queue[TdObject] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        self._ids = itertools.count(1)

    def on_update(self, handler: UpdateHandler) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: UpdateHandler) -> None:
        with contextlib.suppress(ValueError):
            self._handlers.remove(handler)

    async def start(self) -> None:
        if self.client_id is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stop.clear()
        with contextlib.suppress(Exception):
            self._td.execute(json.dumps({"@type": "setLogVerbosityLevel",
                                         "new_verbosity_level": 1}))
        self.client_id = self._td.create_client_id()
        self._thread = threading.Thread(target=self._reader, name="tdjson-receive", daemon=True)
        self._thread.start()
        self._dispatcher = self._loop.create_task(self._dispatch())

    def _reader(self) -> None:
        loop = self._loop
        assert loop is not None
        while not self._stop.is_set():
            raw = self._td.receive(self._receive_timeout)
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("@client_id", self.client_id) != self.client_id:
                continue
            try:
                loop.call_soon_threadsafe(self._route, data)
            except RuntimeError:  # цикл событий уже закрыт
                return

    def _route(self, data: TdObject) -> None:
        extra = data.get("@extra")
        if extra is not None:
            fut = self._pending.pop(str(extra), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        if self._queue is not None:
            self._queue.put_nowait(data)

    async def _dispatch(self) -> None:
        assert self._queue is not None
        while True:
            update = await self._queue.get()
            for handler in list(self._handlers):
                try:
                    await handler(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("ошибка обработки обновления %s", update.get("@type"))

    async def request(self, query: TdObject, *, timeout: float | None = None) -> TdObject:
        if self.client_id is None or self._loop is None:
            raise TransportError("клиент TDLib не запущен")
        extra = f"r{next(self._ids)}"
        fut: asyncio.Future[TdObject] = self._loop.create_future()
        self._pending[extra] = fut
        body = dict(query)
        body["@extra"] = extra
        self._td.send(self.client_id, json.dumps(body, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout or self._request_timeout)
        except TimeoutError:
            self._pending.pop(extra, None)
            raise TransportError(f"TDLib не ответил на {query.get('@type')}") from None
        return raise_for_error(resp)

    async def close(self, timeout: float = 10.0) -> None:
        if self.client_id is None:
            return
        closed = asyncio.Event()

        async def watch(update: TdObject) -> None:
            state = update.get("authorization_state") or {}
            if (update.get("@type") == "updateAuthorizationState"
                    and state.get("@type") == "authorizationStateClosed"):
                closed.set()

        self.on_update(watch)
        with contextlib.suppress(Exception):
            await self.request({"@type": "close"}, timeout=timeout)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(closed.wait(), timeout)
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, self._receive_timeout + 2)
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()
        self._handlers.clear()
        self.client_id = None
