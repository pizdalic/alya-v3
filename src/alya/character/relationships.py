"""Отношения: симпатия (affinity, -1..1) и доверие (trust, 0..1) к человеку в чате."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database

MAX_STEP = 0.05  # одно сообщение не может резко изменить отношение


@dataclass(frozen=True, slots=True)
class Relationship:
    user_id: int
    chat_id: int
    affinity: float = 0.0
    trust: float = 0.3
    interactions: int = 0
    is_owner: bool = False

    def describe(self) -> str:
        if self.is_owner:
            return "это твой парень — самый близкий человек"
        if self.interactions < 3:
            return "почти незнакомый человек"
        a = self.affinity
        feel = ("очень нравится" if a > 0.5 else "симпатичен" if a > 0.15 else
                "нейтрально" if a > -0.15 else "неприятен" if a > -0.5 else "очень неприятен")
        trust = "доверяешь" if self.trust > 0.6 else "не доверяешь" if self.trust < 0.2 else ""
        return ", ".join(x for x in (f"знакомый, {feel}", trust) if x)


def _bounded(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class RelationshipStore:
    def __init__(self, db: Database, owner_user_id: int | None, *, clock: Clock = utcnow) -> None:
        self._db = db
        self._owner = owner_user_id
        self._clock = clock

    def _row(self, conn: sqlite3.Connection, user_id: int, chat_id: int) -> Relationship:
        row = conn.execute(
            "SELECT affinity, trust, interactions FROM relationships "
            "WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)).fetchone()
        owner = self._owner is not None and user_id == self._owner
        if row is None:
            return Relationship(user_id, chat_id, 0.8 if owner else 0.0,
                                0.9 if owner else 0.3, 0, owner)
        return Relationship(user_id, chat_id, row["affinity"], row["trust"],
                            row["interactions"], owner)

    async def get(self, user_id: int, chat_id: int) -> Relationship:
        return await self._db.read(lambda conn: self._row(conn, user_id, chat_id))

    async def interact(self, user_id: int, chat_id: int, *, d_affinity: float = 0.0,
                       d_trust: float = 0.0) -> Relationship:
        now = iso(self._clock())

        def op(conn: sqlite3.Connection) -> Relationship:
            cur = self._row(conn, user_id, chat_id)
            aff = _bounded(cur.affinity + _bounded(d_affinity, -MAX_STEP, MAX_STEP), -1, 1)
            tr = _bounded(cur.trust + _bounded(d_trust, -MAX_STEP, MAX_STEP), 0, 1)
            conn.execute(
                "INSERT INTO relationships (user_id, chat_id, affinity, trust, interactions, "
                "last_interaction_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, chat_id) DO UPDATE SET affinity = excluded.affinity, "
                "trust = excluded.trust, interactions = excluded.interactions, "
                "last_interaction_at = excluded.last_interaction_at, "
                "updated_at = excluded.updated_at",
                (user_id, chat_id, aff, tr, cur.interactions + 1, now, now))
            return Relationship(user_id, chat_id, aff, tr, cur.interactions + 1, cur.is_owner)

        return await self._db.transaction(op)
