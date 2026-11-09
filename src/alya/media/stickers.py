"""Стикеры из разрешённых наборов: хранение и выбор по эмодзи.

Наборы добавляет только владелец (/stickers add <имя>). Аля не шлёт стикеры, которых нет
в таблице, — никакого «поиска стикеров по интернету».
"""

from __future__ import annotations

import random
import re
import sqlite3
from collections.abc import Sequence

from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database

SET_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
MAX_PER_SET = 200

# Если точного эмодзи нет — ищем в той же эмоциональной группе.
_GROUPS: tuple[frozenset[str], ...] = (
    frozenset("😊☺️🙂😄😁😀😃🥰😍😻💖❤️💕🤗")
    | {"☺️", "❤️"},
    frozenset("😂🤣😆😹😝😜"),
    frozenset("😢😭🥺😿😔😞🙁") | {"☹️"},
    frozenset("😳🙈😊😶🫣"),
    frozenset("😠😡😤😾😒🙄"),
    frozenset("😮😲😱🙀😳"),
    frozenset("🤔🧐😕"),
    frozenset("😴😪🥱"),
    frozenset("👋👍👌🙏"),
)


def _norm(emoji: str) -> str:
    return emoji.replace("\ufe0f", "").strip()


class StickerStore:
    def __init__(self, db: Database, *, clock: Clock = utcnow,
                 rng: random.Random | None = None) -> None:
        self._db = db
        self._clock = clock
        self._rng = rng or random.Random()

    async def add_set(self, set_name: str, stickers: Sequence[tuple[str, str]]) -> int:
        """Заменить набор целиком. stickers — (remote_id, emoji). Вернуть число стикеров."""
        if not SET_NAME.match(set_name):
            raise ValueError("некорректное имя набора")
        now = iso(self._clock())
        rows = [(rid, set_name, _norm(emoji)[:16], now)
                for rid, emoji in list(stickers)[:MAX_PER_SET] if rid and len(rid) <= 256]

        def op(conn: sqlite3.Connection) -> int:
            conn.execute("DELETE FROM stickers WHERE set_name = ?", (set_name,))
            conn.executemany(
                "INSERT INTO stickers (remote_id, set_name, emoji, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (remote_id) DO UPDATE SET set_name = excluded.set_name, "
                "emoji = excluded.emoji, added_at = excluded.added_at", rows)
            return len(rows)

        return await self._db.transaction(op)

    async def remove_set(self, set_name: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            return conn.execute("DELETE FROM stickers WHERE set_name = ?", (set_name,)).rowcount

        return await self._db.transaction(op)

    async def sets(self) -> list[tuple[str, int]]:
        rows = await self._db.fetchall(
            "SELECT set_name, count(*) FROM stickers GROUP BY set_name ORDER BY set_name")
        return [(str(r[0]), int(r[1])) for r in rows]

    async def pick(self, emoji: str) -> str | None:
        """remote_id стикера под эмодзи: точное совпадение, иначе из той же группы."""
        want = _norm(emoji)
        if not want:
            return None
        candidates = [want]
        for group in _GROUPS:
            normed = {_norm(e) for e in group} - {""}
            if want in normed:
                candidates += sorted(normed - {want})
                break
        for cand in candidates:
            rows = await self._db.fetchall("SELECT remote_id FROM stickers WHERE emoji = ?",
                                           (cand,))
            if rows:
                return str(self._rng.choice(rows)[0])
        return None
