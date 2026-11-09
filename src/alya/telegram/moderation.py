"""Модерация в группах в режиме dry_run (SECURITY T11).

Аля ничего не удаляет, не глушит и не банит. Она только записывает в audit_log,
что сделала бы (action = "moderation_dry_run"). Текст сообщения в журнал не
попадает — только идентификаторы, правило и причина.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database
from alya.telegram.models import IncomingMessage

LINK = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
MAX_TRACKED = 5000


@dataclass(frozen=True)
class ModerationRules:
    flood_messages: int = 8
    flood_window_s: int = 30
    max_links: int = 3
    banned_words: Sequence[str] = ()
    exempt_user_ids: Sequence[int] = ()


@dataclass(frozen=True)
class Verdict:
    rule: str  # flood | links | banned_words
    action: str  # mute | delete
    reason: str


class Moderator:
    def __init__(self, db: Database, rules: ModerationRules, *, clock: Clock = utcnow) -> None:
        self._db = db
        self._rules = rules
        self._clock = clock
        self._words = tuple(w.strip().lower() for w in rules.banned_words if w.strip())
        self._recent: dict[tuple[int, int], deque[datetime]] = {}
        self._flagged: dict[tuple[int, int], datetime] = {}

    def evaluate(self, msg: IncomingMessage) -> list[Verdict]:
        uid = msg.sender_id
        if not msg.is_group or uid is None or msg.is_outgoing or uid in self._rules.exempt_user_ids:
            return []
        out: list[Verdict] = []
        flood = self._flood(msg.chat_id, uid, msg.date)
        if flood:
            out.append(Verdict("flood", "mute", flood))
        text = f"{msg.text or ''} {msg.caption or ''}"
        links = len(LINK.findall(text))
        if links > self._rules.max_links:
            out.append(Verdict("links", "delete", f"{links} ссылок"))
        low = text.lower()
        hits = sum(1 for w in self._words if w in low)
        if hits:
            out.append(Verdict("banned_words", "delete", f"запрещённых слов: {hits}"))
        return out

    def _flood(self, chat_id: int, uid: int, at: datetime) -> str | None:
        key = (chat_id, uid)
        window = timedelta(seconds=self._rules.flood_window_s)
        if key not in self._recent and len(self._recent) >= MAX_TRACKED:
            self._prune(at - window)
        q = self._recent.setdefault(key, deque())
        q.append(at)
        while q and q[0] < at - window:
            q.popleft()
        if len(q) < self._rules.flood_messages:
            return None
        last = self._flagged.get(key)
        if last is not None and at - last < window:
            return None  # уже отмечен в этом окне — не засоряем журнал
        self._flagged[key] = at
        return f"{len(q)} сообщений за {self._rules.flood_window_s} с"

    def _prune(self, cutoff: datetime) -> None:
        for key in [k for k, q in self._recent.items() if not q or q[-1] < cutoff]:
            self._recent.pop(key, None)
            self._flagged.pop(key, None)
        if len(self._recent) >= MAX_TRACKED:
            self._recent.clear()
            self._flagged.clear()

    async def check(self, msg: IncomingMessage) -> list[Verdict]:
        verdicts = self.evaluate(msg)
        if not verdicts:
            return []
        now = iso(self._clock())
        rows = [(msg.sender_id, "moderation_dry_run", str(msg.chat_id), json.dumps({
            "chat_id": msg.chat_id, "message_id": msg.message_id, "rule": v.rule,
            "would": v.action, "reason": v.reason, "dry_run": True}, ensure_ascii=False), now)
            for v in verdicts]
        await self._db.transaction(lambda c: c.executemany(
            "INSERT INTO audit_log (actor_user_id, action, target, details, created_at) "
            "VALUES (?, ?, ?, ?, ?)", rows))
        return verdicts
