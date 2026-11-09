"""Sender: забирает готовые сообщения из outbox и отправляет их через транспорт."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from alya.core.timeutil import Clock, utcnow
from alya.telegram.outbox import OutboxItem, OutboxStore
from alya.telegram.transport import FloodWait, Transport, TransportError

log = logging.getLogger(__name__)

type OnSent = Callable[[OutboxItem, int], Awaitable[None]]
type Sleep = Callable[[float], Awaitable[None]]


_MEDIA_METHODS = {"voice": "send_voice", "photo": "send_photo", "sticker": "send_sticker",
                  "reaction": "react"}


class _Unsupported(TransportError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"транспорт не умеет отправлять {kind}", permanent=True)


def _cleanup(payload: dict[str, Any]) -> None:
    path = payload.get("path")
    if path:
        with contextlib.suppress(OSError):
            Path(str(path)).unlink(missing_ok=True)


class Sender:
    name = "telegram-sender"

    def __init__(self, store: OutboxStore, transport: Transport, *, on_sent: OnSent | None = None,
                 clock: Clock = utcnow, poll_s: float = 1.0, chat_interval_s: float = 1.0,
                 global_interval_s: float = 0.05, max_attempts: int = 5,
                 sleep: Sleep = asyncio.sleep) -> None:
        self._store = store
        self._transport = transport
        self._on_sent = on_sent
        self._clock = clock
        self._poll = poll_s
        self._chat_interval = timedelta(seconds=chat_interval_s)
        self._global_interval = global_interval_s
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._last_sent: dict[int, datetime] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.paused_until: datetime | None = None

    def wake(self) -> None:
        self._wake.set()

    def _paused(self) -> bool:
        return self.paused_until is not None and self._clock() < self.paused_until

    async def run_once(self) -> int:
        if self._paused():
            return 0
        sent = 0
        for item in await self._store.claim_due():
            if self._paused():
                assert self.paused_until is not None
                await self._store.release(item.id, self.paused_until)
                continue
            last = self._last_sent.get(item.chat_id)
            if last is not None and self._clock() - last < self._chat_interval:
                await self._store.release(item.id, last + self._chat_interval)
                continue
            if await self._send(item):
                sent += 1
            if self._global_interval:
                await self._sleep(self._global_interval)
        return sent

    async def _send(self, item: OutboxItem) -> bool:
        payload = item.payload
        typing_s = float(payload.get("typing_s") or 0)
        if typing_s > 0:
            with contextlib.suppress(Exception):
                await self._transport.send_typing(item.chat_id)
            await self._sleep(typing_s)
        try:
            tg_id = await self._dispatch(item.chat_id, payload)
        except FloodWait as exc:
            self.paused_until = self._clock() + timedelta(seconds=exc.seconds)
            log.warning("FloodWait %.0f с: отправка на паузе", exc.seconds)
            await self._store.release(item.id, self.paused_until)
            return False
        except TransportError as exc:
            await self._fail(item, str(exc), exc.permanent)
            return False
        except asyncio.CancelledError:
            await self._store.release(item.id, self._clock())
            raise
        except Exception as exc:
            await self._fail(item, type(exc).__name__, False)
            return False
        await self._store.mark_sent(item.id)
        _cleanup(payload)
        self._last_sent[item.chat_id] = self._clock()
        if self._on_sent is not None:
            try:
                await self._on_sent(item, tg_id)
            except Exception:
                log.exception("ошибка после отправки")
        return True

    async def _dispatch(self, chat_id: int, payload: dict[str, Any]) -> int:
        kind = str(payload.get("kind") or "text")
        reply_to = payload.get("reply_to")
        if kind == "text":
            return await self._transport.send_text(chat_id, str(payload["text"]),
                                                   reply_to=reply_to)
        method = getattr(self._transport, _MEDIA_METHODS.get(kind, ""), None)
        if method is None:
            raise _Unsupported(kind)
        if kind == "voice":
            if not Path(str(payload.get("path"))).is_file():
                raise TransportError("файл голосового пропал", permanent=True)
            return int(await method(chat_id, str(payload["path"]),
                                    int(payload.get("duration_s") or 1), reply_to=reply_to))
        if kind == "photo":
            if not Path(str(payload.get("path"))).is_file():
                raise TransportError("файл фото пропал", permanent=True)
            return int(await method(chat_id, str(payload["path"]),
                                    caption=str(payload.get("caption") or ""), reply_to=reply_to))
        if kind == "sticker":
            return int(await method(chat_id, str(payload["remote_id"]), reply_to=reply_to))
        message_id = int(payload["message_id"])
        await method(chat_id, message_id, str(payload["emoji"]))
        return message_id

    async def _fail(self, item: OutboxItem, error: str, permanent: bool) -> None:
        if permanent or item.attempts + 1 >= self._max_attempts:
            log.warning("сообщение в чат %s не отправлено: %s", item.chat_id, error)
            await self._store.mark_failed(item.id, error)
            _cleanup(item.payload)
        else:
            await self._store.mark_retry(item.id, error, min(2.0 * 2 ** item.attempts, 300.0))

    async def _loop(self) -> None:
        while True:
            try:
                sent = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка цикла отправки")
                sent = 0
            if sent:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll)

    async def start(self) -> None:
        await self._store.recover()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
