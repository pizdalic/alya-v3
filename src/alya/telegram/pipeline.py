"""Поток входящего сообщения: запись → фильтры → debounce → адресат → персонаж → outbox."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from alya.character.allocator import HistoryLine
from alya.character.engine import Turn
from alya.core.timeutil import Clock, iso, utcnow
from alya.memory.models import MemoryContext
from alya.storage.db import Database
from alya.storage.entities import add_member, upsert_chat, upsert_user
from alya.telegram.addressee import AddresseeDetector, Decision
from alya.telegram.debounce import Debouncer
from alya.telegram.models import IncomingMessage, MessageEdited, MessagesDeleted
from alya.telegram.outbox import OutboxItem, OutboxStore
from alya.telegram.sanitize import clean_outgoing
from alya.telegram.transport import Transport

log = logging.getLogger(__name__)

type Strangers = Literal["known", "all", "owner_only"]
_EXPLICIT = frozenset({"reply", "mention", "name", "classifier"})


class Responder(Protocol):
    async def respond(self, turn: Turn) -> object: ...


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    me_id: int
    owner_id: int | None = None
    strangers: Strangers = "known"
    group_context_hours: int = 48
    private_history_days: int = 30
    replies_per_10min: int = 20
    typing_chars_per_s: float = 12.0
    max_typing_delay_s: float = 6.0
    history_lines: int = 30
    debounce_s: float = 2.0
    debounce_max_wait_s: float = 8.0


class Pipeline:
    def __init__(self, db: Database, engine: Responder, detector: AddresseeDetector,
                 outbox: OutboxStore, *, config: PipelineConfig,
                 transport: Transport | None = None, wake: Callable[[], None] | None = None,
                 clock: Clock = utcnow) -> None:
        self._db = db
        self._engine = engine
        self._detector = detector
        self._outbox = outbox
        self._cfg = config
        self._transport = transport
        self._wake = wake
        self._clock = clock
        self._replies: dict[int, deque[datetime]] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._debouncer: Debouncer[tuple[int, int], IncomingMessage] = Debouncer(
            self._handle_batch, delay_s=config.debounce_s, max_wait_s=config.debounce_max_wait_s)

    def set_wake(self, wake: Callable[[], None]) -> None:
        self._wake = wake

    # --- хранение ----------------------------------------------------------

    async def _store(self, msg: IncomingMessage, *, sender_id: int | None) -> None:
        now = iso(self._clock())
        ttl = (timedelta(days=self._cfg.private_history_days) if msg.is_private
               else timedelta(hours=self._cfg.group_context_hours))
        expires = iso(msg.date + ttl)

        def op(conn: sqlite3.Connection) -> None:
            upsert_chat(conn, msg.chat_id, msg.chat_kind, now, title=msg.chat_title)
            if sender_id is not None:
                upsert_user(conn, sender_id, now, username=msg.sender_username,
                            display_name=msg.sender_name or None)
                if not msg.is_private:
                    add_member(conn, msg.chat_id, sender_id, now)
            conn.execute(
                """INSERT INTO messages (chat_id, tg_message_id, sender_user_id, is_outgoing,
                       reply_to_tg_message_id, addressed_to_alya, context_only, text, media_ref,
                       created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                   ON CONFLICT (chat_id, tg_message_id) DO NOTHING""",
                (msg.chat_id, msg.message_id, sender_id, int(msg.is_outgoing),
                 msg.reply_to_message_id, int(not msg.is_private and not msg.is_outgoing),
                 msg.text, msg.media_kind, iso(msg.date), expires))

        await self._db.transaction(op)

    async def _mark_addressed(self, chat_id: int, ids: Sequence[int], date: datetime) -> None:
        expires = iso(date + timedelta(days=self._cfg.private_history_days))
        marks = ",".join("?" * len(ids))
        await self._db.execute(
            f"UPDATE messages SET addressed_to_alya = 1, context_only = 0, expires_at = ? "
            f"WHERE chat_id = ? AND tg_message_id IN ({marks})", (expires, chat_id, *ids))

    async def _allowed(self, msg: IncomingMessage) -> bool:
        uid = msg.sender_id
        assert uid is not None
        rows = await self._db.fetchall("SELECT is_blocked FROM users WHERE user_id = ?", (uid,))
        if rows and rows[0][0]:
            return False
        if not msg.is_private or uid == self._cfg.owner_id:
            return True
        policy = self._cfg.strangers
        if policy == "all":
            return True
        if policy == "owner_only":
            return False
        if msg.sender_is_contact:
            return True
        shared = await self._db.fetchall(
            "SELECT 1 FROM chat_members m JOIN chats c ON c.chat_id = m.chat_id "
            "WHERE m.user_id = ? AND c.kind IN ('group', 'supergroup') LIMIT 1", (uid,))
        return bool(shared)

    # --- входящие ----------------------------------------------------------

    async def on_message(self, msg: IncomingMessage) -> None:
        if msg.chat_kind == "channel":
            return  # каналы — Phase 7
        if msg.is_outgoing:
            # владелец написал с аккаунта Али вручную
            await self._store(msg, sender_id=self._cfg.me_id)
            if msg.is_group:
                targets = [msg.reply_to_user_id] if msg.reply_to_user_id else []
                self._detector.note_alya_message(msg.chat_id, msg.text, targets)
            return
        if msg.sender_id is None or msg.sender_id == self._cfg.me_id:
            return
        if not await self._allowed(msg):
            return
        await self._store(msg, sender_id=msg.sender_id)
        self._debouncer.add((msg.chat_id, msg.sender_id), msg)

    async def on_edit(self, ev: MessageEdited) -> None:
        await self._db.execute(
            "UPDATE messages SET text = ?, edited_at = ? WHERE chat_id = ? AND tg_message_id = ?",
            (ev.text, iso(ev.edited_at), ev.chat_id, ev.message_id))

    async def on_delete(self, ev: MessagesDeleted) -> None:
        if not ev.message_ids:
            return
        marks = ",".join("?" * len(ev.message_ids))
        await self._db.execute(
            f"DELETE FROM messages WHERE chat_id = ? AND tg_message_id IN ({marks})",
            (ev.chat_id, *ev.message_ids))

    async def _history(self, chat_id: int, exclude: Sequence[int]) -> list[HistoryLine]:
        marks = ",".join("?" * len(exclude)) or "NULL"
        rows = await self._db.fetchall(
            f"""SELECT m.is_outgoing, COALESCE(u.display_name, u.username, 'кто-то'), m.text
                FROM messages m LEFT JOIN users u ON u.user_id = m.sender_user_id
                WHERE m.chat_id = ? AND m.text IS NOT NULL AND m.tg_message_id NOT IN ({marks})
                  AND (m.expires_at IS NULL OR m.expires_at > ?)
                ORDER BY m.created_at DESC, m.id DESC LIMIT ?""",
            (chat_id, *exclude, iso(self._clock()), self._cfg.history_lines))
        return [HistoryLine("ты" if r[0] else r[1], r[2], bool(r[0])) for r in reversed(rows)]

    def _rate_ok(self, chat_id: int) -> bool:
        now = self._clock()
        window = self._replies.setdefault(chat_id, deque())
        while window and now - window[0] > timedelta(minutes=10):
            window.popleft()
        if len(window) >= self._cfg.replies_per_10min:
            return False
        window.append(now)
        return True

    async def _handle_batch(self, key: tuple[int, int], msgs: list[IncomingMessage]) -> None:
        chat_id, sender_id = key
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._respond(chat_id, sender_id, msgs)

    async def _respond(self, chat_id: int, sender_id: int, msgs: list[IncomingMessage]) -> None:
        decisions: list[Decision] = [await self._detector.decide(m) for m in msgs]
        chosen = next((d for d in decisions if d.addressed), None)
        if chosen is None:
            return
        last = msgs[-1]
        ids = [m.message_id for m in msgs]
        await self._mark_addressed(chat_id, ids, last.date)
        if not self._rate_ok(chat_id):
            log.warning("лимит ответов в чате %s: пропускаю", chat_id)
            return
        text = "\n".join(m.text for m in msgs if m.text)
        ctx = (MemoryContext.private(sender_id, chat_id) if last.is_private
               else MemoryContext.group(chat_id, sender_id))
        turn = Turn(ctx, text, last.display_name, await self._history(chat_id, ids), True)
        reply = await self._engine.respond(turn)
        parts = [p for raw in getattr(reply, "messages", []) for p in clean_outgoing(raw)]
        if self._transport is not None:
            try:
                await self._transport.mark_read(chat_id, ids)
            except Exception as exc:
                log.debug("не удалось отметить прочитанным: %s", exc)
        if not parts:
            return
        quote = last.is_group and chosen.reason in _EXPLICIT
        for i, part in enumerate(parts):
            typing = min(len(part) / self._cfg.typing_chars_per_s, self._cfg.max_typing_delay_s)
            await self._outbox.enqueue(chat_id, f"{chat_id}:{last.message_id}:{i}", {
                "text": part,
                "reply_to": last.message_id if (quote and i == 0) else None,
                "typing_s": round(typing, 2),
                "addressees": [sender_id],
            })
        if self._wake is not None:
            self._wake()

    async def on_sent(self, item: OutboxItem, tg_id: int) -> None:
        text = str(item.payload.get("text", ""))
        rows = await self._db.fetchall("SELECT kind FROM chats WHERE chat_id = ?", (item.chat_id,))
        kind = rows[0][0] if rows else "private"
        now = self._clock()
        out = IncomingMessage(chat_id=item.chat_id, message_id=tg_id, sender_id=self._cfg.me_id,
                              chat_kind=kind, text=text, date=now, is_outgoing=True,
                              reply_to_message_id=item.payload.get("reply_to"))
        await self._store(replace(out), sender_id=self._cfg.me_id)
        if kind in ("group", "supergroup"):
            self._detector.note_alya_message(item.chat_id, text,
                                             [int(a) for a in item.payload.get("addressees", [])])

    async def drain(self) -> None:
        await self._debouncer.drain()

    async def close(self) -> None:
        await self._debouncer.close()
