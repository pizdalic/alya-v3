"""Хранилище автономии: инициативы, напоминания, пауза, каналы.

Все времена — строки iso() в UTC одного формата, поэтому сравниваются как строки.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from alya.core.timeutil import Clock, iso, parse_iso, utcnow
from alya.storage.db import Database

type InitiativeKind = Literal["dm", "group"]


@dataclass(frozen=True, slots=True)
class DmCandidate:
    chat_id: int
    user_id: int
    name: str
    is_owner: bool
    affinity: float
    trust: float
    interactions: int
    last_message_at: datetime


@dataclass(frozen=True, slots=True)
class GroupActivity:
    chat_id: int
    title: str
    messages: int
    participants: int
    last_message_at: datetime
    alya_last_at: datetime | None


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    chat_id: int
    user_id: int
    text: str
    due_at: datetime


class AutonomyStore:
    def __init__(self, db: Database, *, clock: Clock = utcnow) -> None:
        self._db = db
        self._clock = clock

    # --- инициативы --------------------------------------------------------

    async def record_initiative(self, chat_id: int, user_id: int | None, kind: InitiativeKind,
                                reason: str = "") -> int:
        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO initiatives (chat_id, user_id, kind, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, kind, " ".join(reason.split())[:300], iso(self._clock())))
            return int(cur.lastrowid or 0)

        return await self._db.transaction(op)

    async def last_initiative(self, chat_id: int) -> datetime | None:
        rows = await self._db.fetchall(
            "SELECT MAX(created_at) FROM initiatives WHERE chat_id = ?", (chat_id,))
        return parse_iso(rows[0][0]) if rows and rows[0][0] else None

    async def initiatives_since(self, kind: InitiativeKind, since: datetime) -> int:
        rows = await self._db.fetchall(
            "SELECT COUNT(*) FROM initiatives WHERE kind = ? AND created_at >= ?",
            (kind, iso(since)))
        return int(rows[0][0])

    async def unanswered(self, chat_id: int) -> bool:
        """Последняя инициатива в чате осталась без ответа (нет входящих после неё)."""
        last = await self.last_initiative(chat_id)
        if last is None:
            return False
        rows = await self._db.fetchall(
            "SELECT 1 FROM messages WHERE chat_id = ? AND is_outgoing = 0 AND created_at > ? "
            "LIMIT 1", (chat_id, iso(last)))
        return not rows

    # --- /pause ---------------------------------------------------------------

    async def pause(self, user_id: int) -> None:
        await self._db.execute(
            "INSERT INTO proactive_pauses (user_id, paused_at) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO NOTHING", (user_id, iso(self._clock())))

    async def resume(self, user_id: int) -> None:
        await self._db.execute("DELETE FROM proactive_pauses WHERE user_id = ?", (user_id,))

    async def is_paused(self, user_id: int) -> bool:
        rows = await self._db.fetchall(
            "SELECT 1 FROM proactive_pauses WHERE user_id = ?", (user_id,))
        return bool(rows)

    # --- кандидаты ---------------------------------------------------------

    async def dm_candidates(self, *, owner_id: int | None, min_interactions: int,
                            silent_since: datetime, active_since: datetime) -> list[DmCandidate]:
        """Лички с устоявшимся диалогом, где давно тихо, но не заброшено совсем.

        Исключены: заблокированные, поставившие /pause, чаты без сообщений от человека.
        """
        rows = await self._db.fetchall(
            """SELECT c.chat_id, u.user_id, COALESCE(u.display_name, u.username, '') AS name,
                      COALESCE(r.affinity, 0), COALESCE(r.trust, 0.3),
                      COALESCE(r.interactions, 0),
                      (SELECT MAX(m.created_at) FROM messages m WHERE m.chat_id = c.chat_id),
                      (SELECT MAX(m.created_at) FROM messages m
                        WHERE m.chat_id = c.chat_id AND m.is_outgoing = 0)
               FROM chats c
               JOIN users u ON u.user_id = c.chat_id
               LEFT JOIN relationships r ON r.user_id = u.user_id AND r.chat_id = c.chat_id
               WHERE c.kind = 'private' AND u.is_blocked = 0
                 AND NOT EXISTS (SELECT 1 FROM proactive_pauses p WHERE p.user_id = u.user_id)""")
        out: list[DmCandidate] = []
        for chat_id, user_id, name, aff, trust, inter, last_any, last_in in rows:
            is_owner = owner_id is not None and user_id == owner_id
            if not last_any or not last_in:
                continue
            if not is_owner and inter < min_interactions:
                continue
            last = parse_iso(last_any)
            if last > silent_since or parse_iso(last_in) < active_since:
                continue
            out.append(DmCandidate(chat_id, user_id, name or "собеседник", is_owner,
                                   float(aff), float(trust), int(inter), last))
        return out

    async def group_activity(self, since: datetime, *, me_id: int) -> list[GroupActivity]:
        rows = await self._db.fetchall(
            """SELECT c.chat_id, COALESCE(c.title, ''),
                      SUM(CASE WHEN m.is_outgoing = 0 THEN 1 ELSE 0 END),
                      COUNT(DISTINCT CASE WHEN m.is_outgoing = 0 THEN m.sender_user_id END),
                      MAX(m.created_at),
                      MAX(CASE WHEN m.is_outgoing = 1 OR m.sender_user_id = ?
                               THEN m.created_at END)
               FROM chats c JOIN messages m ON m.chat_id = c.chat_id
               WHERE c.kind IN ('group', 'supergroup') AND m.created_at >= ?
               GROUP BY c.chat_id""", (me_id, iso(since)))
        return [GroupActivity(int(r[0]), r[1], int(r[2] or 0), int(r[3] or 0), parse_iso(r[4]),
                              parse_iso(r[5]) if r[5] else None) for r in rows]

    async def recent_texts(self, chat_id: int, since: datetime, limit: int = 30) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT text FROM messages WHERE chat_id = ? AND created_at >= ? AND text IS NOT NULL "
            "AND is_outgoing = 0 ORDER BY created_at DESC LIMIT ?", (chat_id, iso(since), limit))
        return [r[0] for r in reversed(rows)]

    # --- напоминания ------------------------------------------------------

    async def add_reminder(self, chat_id: int, user_id: int, text: str, due_at: datetime) -> int:
        clean = " ".join(text.split())[:300]
        if not clean:
            raise ValueError("пустое напоминание")

        def op(conn: sqlite3.Connection) -> int:
            dup = conn.execute(
                "SELECT id FROM reminders WHERE chat_id = ? AND user_id = ? AND text = ? "
                "AND due_at = ? AND done_at IS NULL", (chat_id, user_id, clean, iso(due_at))
            ).fetchone()
            if dup:
                return int(dup[0])
            cur = conn.execute(
                "INSERT INTO reminders (chat_id, user_id, text, due_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (chat_id, user_id, clean, iso(due_at),
                                         iso(self._clock())))
            return int(cur.lastrowid or 0)

        return await self._db.transaction(op)

    async def due_reminders(self, limit: int = 20) -> list[Reminder]:
        rows = await self._db.fetchall(
            "SELECT id, chat_id, user_id, text, due_at FROM reminders "
            "WHERE done_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT ?",
            (iso(self._clock()), limit))
        return [Reminder(int(r[0]), int(r[1]), int(r[2]), r[3], parse_iso(r[4])) for r in rows]

    async def pending_reminders(self, user_id: int, limit: int = 20) -> list[Reminder]:
        rows = await self._db.fetchall(
            "SELECT id, chat_id, user_id, text, due_at FROM reminders "
            "WHERE done_at IS NULL AND user_id = ? ORDER BY due_at LIMIT ?", (user_id, limit))
        return [Reminder(int(r[0]), int(r[1]), int(r[2]), r[3], parse_iso(r[4])) for r in rows]

    async def finish_reminder(self, reminder_id: int) -> None:
        await self._db.execute("UPDATE reminders SET done_at = ? WHERE id = ? AND done_at IS NULL",
                               (iso(self._clock()), reminder_id))

    async def cancel_reminder(self, user_id: int, reminder_id: int) -> bool:
        n = await self._db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ? AND done_at IS NULL",
            (reminder_id, user_id))
        return n > 0

    async def open_reminders_count(self, user_id: int) -> int:
        rows = await self._db.fetchall(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND done_at IS NULL", (user_id,))
        return int(rows[0][0])

    # --- каналы -------------------------------------------------------------

    async def channel_muted(self, chat_id: int) -> bool:
        rows = await self._db.fetchall("SELECT muted FROM channel_state WHERE chat_id = ?",
                                       (chat_id,))
        return bool(rows and rows[0][0])

    async def set_channel_muted(self, chat_id: int, muted: bool) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            if not conn.execute("SELECT 1 FROM chats WHERE chat_id = ? AND kind = 'channel'",
                                (chat_id,)).fetchone():
                return False
            conn.execute(
                "INSERT INTO channel_state (chat_id, muted) VALUES (?, ?) "
                "ON CONFLICT (chat_id) DO UPDATE SET muted = excluded.muted",
                (chat_id, int(muted)))
            return True

        return await self._db.transaction(op)

    async def channels(self) -> list[tuple[int, str, bool, int]]:
        """(chat_id, название, выключен, постов в базе)."""
        rows = await self._db.fetchall(
            """SELECT c.chat_id, COALESCE(c.title, ''), COALESCE(s.muted, 0),
                      (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.chat_id)
               FROM chats c LEFT JOIN channel_state s ON s.chat_id = c.chat_id
               WHERE c.kind = 'channel' ORDER BY c.title""")
        return [(int(r[0]), r[1], bool(r[2]), int(r[3])) for r in rows]

    async def channels_for_digest(self, older_than: datetime) -> list[tuple[int, str, datetime | None]]:
        rows = await self._db.fetchall(
            """SELECT c.chat_id, COALESCE(c.title, ''), s.last_digest_at
               FROM chats c LEFT JOIN channel_state s ON s.chat_id = c.chat_id
               WHERE c.kind = 'channel' AND COALESCE(s.muted, 0) = 0
                 AND (s.last_digest_at IS NULL OR s.last_digest_at <= ?)""", (iso(older_than),))
        return [(int(r[0]), r[1], parse_iso(r[2]) if r[2] else None) for r in rows]

    async def channel_posts(self, chat_id: int, since: datetime | None,
                            limit: int = 40) -> list[str]:
        floor = iso(since) if since is not None else iso(self._clock() - timedelta(days=3))
        rows = await self._db.fetchall(
            "SELECT text FROM messages WHERE chat_id = ? AND created_at > ? AND text IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?", (chat_id, floor, limit))
        return [r[0] for r in reversed(rows)]

    async def mark_digested(self, chat_id: int) -> None:
        await self._db.execute(
            "INSERT INTO channel_state (chat_id, last_digest_at) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET last_digest_at = excluded.last_digest_at",
            (chat_id, iso(self._clock())))
