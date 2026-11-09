"""Лимиты частоты медиа-действий. Журнал в БД, поэтому перезапуск их не сбрасывает."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal

from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database

type UsageKind = Literal["vision", "stt", "tts", "image", "search", "reaction", "sticker"]
KINDS: tuple[str, ...] = ("vision", "stt", "tts", "image", "search", "reaction", "sticker")
_KEEP = timedelta(days=2)


class UsageLimiter:
    """per_hour[kind] — сколько раз за последний час (всего по всем чатам); 0 — выключено.

    per_day — суточный потолок (защита бюджета), нет ключа — без суточного лимита.
    """

    def __init__(self, db: Database, per_hour: Mapping[str, int], *,
                 per_day: Mapping[str, int] | None = None, clock: Clock = utcnow) -> None:
        unknown = sorted((set(per_hour) | set(per_day or {})) - set(KINDS))
        if unknown:
            raise ValueError(f"неизвестные виды лимитов: {', '.join(unknown)}")
        self._db = db
        self._hour = dict(per_hour)
        self._day = dict(per_day or {})
        self._clock = clock

    def enabled(self, kind: UsageKind) -> bool:
        return self._hour.get(kind, 0) > 0

    async def take(self, kind: UsageKind, chat_id: int | None = None) -> bool:
        """Атомарно проверить лимит и записать использование. False — лимит исчерпан."""
        hour_limit = self._hour.get(kind, 0)
        if hour_limit <= 0:
            return False
        day_limit = self._day.get(kind)
        now = self._clock()

        def op(conn: sqlite3.Connection) -> bool:
            conn.execute("DELETE FROM media_usage WHERE at < ?", (iso(now - _KEEP),))
            used_hour = conn.execute(
                "SELECT count(*) FROM media_usage WHERE kind = ? AND at > ?",
                (kind, iso(now - timedelta(hours=1)))).fetchone()[0]
            if used_hour >= hour_limit:
                return False
            if day_limit is not None:
                used_day = conn.execute(
                    "SELECT count(*) FROM media_usage WHERE kind = ? AND at > ?",
                    (kind, iso(now - timedelta(days=1)))).fetchone()[0]
                if used_day >= day_limit:
                    return False
            conn.execute("INSERT INTO media_usage (kind, chat_id, at) VALUES (?, ?, ?)",
                         (kind, chat_id, iso(now)))
            return True

        return await self._db.transaction(op)

    async def check(self, kind: UsageKind) -> str | None:
        """Почему take() сейчас вернёт False, или None. Ничего не записывает."""
        hour_limit = self._hour.get(kind, 0)
        if hour_limit <= 0:
            return f"{kind} выключено"
        used = await self.used(kind, hours=1)
        if used >= hour_limit:
            return f"лимит {kind} за час исчерпан ({used}/{hour_limit})"
        day_limit = self._day.get(kind)
        if day_limit is not None:
            used_day = await self.used(kind, hours=24)
            if used_day >= day_limit:
                return f"суточный лимит {kind} исчерпан ({used_day}/{day_limit})"
        return None

    async def used(self, kind: UsageKind, *, hours: int = 24) -> int:
        rows = await self._db.fetchall(
            "SELECT count(*) FROM media_usage WHERE kind = ? AND at > ?",
            (kind, iso(self._clock() - timedelta(hours=hours))))
        return int(rows[0][0])
