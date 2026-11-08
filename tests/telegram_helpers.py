"""Фейки и фикстуры для тестов Telegram-слоя (без сети и без libtdjson)."""

from __future__ import annotations

import itertools
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alya.storage.db import Database
from alya.storage.migrator import apply_migrations
from alya.telegram.models import IncomingMessage
from tests.memory_helpers import FakeClock

_ids = itertools.count(1)


def msg(chat_id: int, sender: int | None, text: str, *, at: datetime, kind: str = "supergroup",
        name: str | None = None, **kw: Any) -> IncomingMessage:
    return IncomingMessage(chat_id=chat_id, message_id=next(_ids) << 20, sender_id=sender,
                           chat_kind=kind, text=text, date=at,  # type: ignore[arg-type]
                           sender_name=name or f"u{sender}", **kw)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.typing: list[int] = []
        self.read: list[tuple[int, list[int]]] = []
        self.errors: list[Exception] = []
        self._ids = itertools.count(1)

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text, reply_to))
        return (10_000 + next(self._ids)) << 20

    async def send_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)

    async def mark_read(self, chat_id: int, message_ids: Sequence[int]) -> None:
        self.read.append((chat_id, list(message_ids)))


class FakeEngine:
    def __init__(self, replies: list[list[str]] | None = None) -> None:
        self.replies = list(replies or [])
        self.turns: list[Any] = []

    async def respond(self, turn: Any) -> SimpleNamespace:
        self.turns.append(turn)
        return SimpleNamespace(messages=self.replies.pop(0) if self.replies else ["ок"])


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.sqlite3")
        self.db.connect()
        apply_migrations(self.db.conn)
        self.clock = FakeClock()

    async def asyncTearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()
