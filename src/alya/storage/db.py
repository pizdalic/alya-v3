"""SQLite: WAL, внешние ключи, busy_timeout, один пишущий поток через asyncio.Lock."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from alya.core.errors import StorageError

type Params = Sequence[Any]

MEMORY = ":memory:"


class Database:
    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self._path = str(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("БД не открыта")
        return self._conn

    def connect(self) -> None:
        if self._conn is not None:
            return
        if self._path != MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: транзакциями управляем явно (BEGIN IMMEDIATE / COMMIT).
        # check_same_thread=False: доступ сериализован self._lock, вызовы идут через to_thread.
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        if self._path != MEMORY:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.close()
                raise StorageError(f"не удалось включить WAL (journal_mode={mode})")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def fts5_available(self) -> bool:
        try:
            self.conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
            self.conn.execute("DROP TABLE temp._fts5_probe")
        except sqlite3.OperationalError:
            return False
        return True

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._fetchall, sql, params)

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Выполнить одну команду в транзакции. Возвращает rowcount."""
        return await self.transaction(lambda c: c.execute(sql, params).rowcount)

    async def transaction[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Выполнить fn атомарно: при исключении — ROLLBACK."""
        async with self._lock:
            return await asyncio.to_thread(self._transaction, fn)

    def _fetchall(self, sql: str, params: Params) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _transaction[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result
