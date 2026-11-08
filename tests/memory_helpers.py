"""Общие фикстуры для тестов памяти."""

from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from alya.core.timeutil import iso
from alya.memory.mirror import MarkdownMirror
from alya.memory.repository import LayerPolicy, MemoryRepository
from alya.memory.search import MemorySearch
from alya.memory.service import MemoryService
from alya.memory.vectors import Vector
from alya.storage.db import Database
from alya.storage.entities import add_member, upsert_chat, upsert_user
from alya.storage.migrator import apply_migrations

A, B, C = 101, 202, 303
GROUP, GROUP2 = -1001, -1002


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class HashEmbedder:
    """Детерминированный «мешок слов»: общие слова → близкие векторы."""

    def __init__(self, dim: int = 64, model_id: str = "test-embed") -> None:
        self.dim = dim
        self._model = model_id
        self.calls = 0
        self.fail = False

    @property
    def model_id(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        out = []
        for text in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for word in re.findall(r"\w+", text.lower()):
                h = int(hashlib.sha256(word[:5].encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            out.append(v)
        return out


class MemoryTestCase(unittest.IsolatedAsyncioTestCase):
    use_embedder = False

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(root / "t.sqlite3")
        self.db.connect()
        apply_migrations(self.db.conn)
        self.clock = FakeClock()
        now = iso(self.clock())

        def seed(conn):  # type: ignore[no-untyped-def]
            for uid in (A, B, C):
                upsert_user(conn, uid, now)
                upsert_chat(conn, uid, "private", now)
                add_member(conn, uid, uid, now)
            upsert_chat(conn, GROUP, "supergroup", now, title="g")
            upsert_chat(conn, GROUP2, "supergroup", now, title="g2")
            for uid in (A, B, C):
                add_member(conn, GROUP, uid, now)
            add_member(conn, GROUP2, A, now)

        await self.db.transaction(seed)
        self.repo = MemoryRepository(self.db, LayerPolicy(), clock=self.clock)
        self.embedder = HashEmbedder() if self.use_embedder else None
        self.search = MemorySearch(self.repo, self.embedder, clock=self.clock)
        self.mirror = MarkdownMirror(root / "mirror")
        self.service = MemoryService(self.repo, self.search, self.mirror)

    async def asyncTearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()
