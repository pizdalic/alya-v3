"""Outbox: исходящие сообщения живут в БД до подтверждения отправки.

- idempotency_key не даёт поставить одну и ту же часть ответа дважды;
- внутри чата строгий порядок: следующая часть ждёт, пока предыдущая не уйдёт;
- после перезапуска «sending» возвращается в очередь. Если процесс упал между
  отправкой и отметкой, возможен один дубль — лучше, чем потерять сообщение.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: int
    chat_id: int
    key: str
    payload: dict[str, Any]
    attempts: int


class OutboxStore:
    def __init__(self, db: Database, *, clock: Clock = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def enqueue(self, chat_id: int, key: str, payload: dict[str, Any], *,
                      not_before: datetime | None = None) -> bool:
        now = iso(self._clock())
        due = iso(not_before) if not_before is not None else now

        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """INSERT INTO outbox (chat_id, idempotency_key, payload, next_attempt_at,
                                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (idempotency_key) DO NOTHING""",
                (chat_id, key, json.dumps(payload, ensure_ascii=False), due, now, now))
            return cur.rowcount > 0

        return await self._db.transaction(op)

    async def recover(self) -> int:
        now = iso(self._clock())
        return await self._db.transaction(lambda conn: conn.execute(
            "UPDATE outbox SET status = 'pending', updated_at = ? WHERE status = 'sending'",
            (now,)).rowcount)

    async def claim_due(self, limit: int = 10) -> list[OutboxItem]:
        now = iso(self._clock())

        def op(conn: sqlite3.Connection) -> list[OutboxItem]:
            rows = conn.execute(
                """SELECT o.id, o.chat_id, o.idempotency_key, o.payload, o.attempts FROM outbox o
                   WHERE o.status = 'pending' AND o.next_attempt_at <= ?
                     AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.chat_id = o.chat_id
                                     AND p.status IN ('pending', 'sending') AND p.id < o.id)
                   ORDER BY o.id LIMIT ?""", (now, limit)).fetchall()
            for row in rows:
                conn.execute("UPDATE outbox SET status = 'sending', updated_at = ? WHERE id = ?",
                             (now, row[0]))
            return [OutboxItem(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

        return await self._db.transaction(op)

    async def _set(self, sql: str, params: tuple[Any, ...]) -> None:
        await self._db.transaction(lambda conn: conn.execute(sql, params))

    async def mark_sent(self, item_id: int) -> None:
        await self._set("UPDATE outbox SET status = 'sent', attempts = attempts + 1, "
                        "last_error = NULL, updated_at = ? WHERE id = ?",
                        (iso(self._clock()), item_id))

    async def mark_retry(self, item_id: int, error: str, delay_s: float) -> None:
        now = self._clock()
        await self._set("UPDATE outbox SET status = 'pending', attempts = attempts + 1, "
                        "last_error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                        (error[:300], iso(now + timedelta(seconds=delay_s)), iso(now), item_id))

    async def release(self, item_id: int, not_before: datetime) -> None:
        """Вернуть в очередь без траты попытки (FloodWait, интервал чата)."""
        await self._set("UPDATE outbox SET status = 'pending', next_attempt_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (iso(not_before), iso(self._clock()), item_id))

    async def mark_failed(self, item_id: int, error: str) -> None:
        await self._set("UPDATE outbox SET status = 'failed', attempts = attempts + 1, "
                        "last_error = ?, updated_at = ? WHERE id = ?",
                        (error[:300], iso(self._clock()), item_id))

    async def cancel_chat(self, chat_id: int) -> int:
        now = iso(self._clock())
        return await self._db.transaction(lambda conn: conn.execute(
            "UPDATE outbox SET status = 'cancelled', updated_at = ? "
            "WHERE chat_id = ? AND status = 'pending'", (now, chat_id)).rowcount)

    async def counts(self) -> dict[str, int]:
        rows = await self._db.fetchall("SELECT status, COUNT(*) FROM outbox GROUP BY status")
        return {r[0]: r[1] for r in rows}
