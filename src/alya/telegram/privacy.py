"""Команды приватности (Phase 8). Работают в личке у любого собеседника.

/privacy — список команд
/mydata — что Аля о тебе помнит: сколько записей и сообщений, последние записи
/export — все записи о тебе текстом
/forget <id> — забыть одну запись (id из /mydata или /export, можно первые 8 символов)
/forget_me — забыть всё о тебе; выполняется только как «/forget_me да»

Владелец дополнительно: /forget_user <id> [да], /forget_chat <id> [да] — без «да»
только показывает, что будет удалено.

Команды не вызывают модель, не попадают в историю и память; удаление идёт через
Forget API (все хранилища + журнал удалений, который переживает restore из бэкапа).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from alya.core.timeutil import Clock, utcnow
from alya.memory.forget import ForgetReport
from alya.memory.models import MemoryContext
from alya.storage.db import Database
from alya.telegram.models import IncomingMessage

log = logging.getLogger(__name__)

NAMES = frozenset({"/privacy", "/mydata", "/export", "/forget", "/forget_me",
                   "/forget_user", "/forget_chat"})
OWNER_ONLY = frozenset({"/forget_user", "/forget_chat"})
YES = frozenset({"да", "yes", "подтверждаю"})
PART_LIMIT = 3500
MAX_PARTS = 8
MIN_PREFIX = 6

HELP = ("приватность:\n"
        "/mydata — что я о тебе помню\n"
        "/export — все записи о тебе текстом\n"
        "/forget <id> — забыть одну запись\n"
        "/forget_me да — забыть о тебе всё (записи, переписку, отношение)")

OWNER_HELP = ("\n/forget_user <id> да — забыть человека\n"
              "/forget_chat <id> да — забыть чат целиком")

_SCOPE = {"private": "личка", "chat": "группа", "character": "моё"}


class Forgetter(Protocol):
    async def forget_memory(self, ctx: MemoryContext, memory_id: str, *,
                            is_admin: bool = False) -> ForgetReport | None: ...

    async def forget_user(self, user_id: int, *, requested_by: int | None) -> ForgetReport: ...

    async def forget_chat(self, chat_id: int, *, requested_by: int | None) -> ForgetReport: ...


def _about(uid: int) -> tuple[str, list[Any]]:
    """Записи о человеке: его личные, групповые о нём, записи его личного чата."""
    return ("(m.owner_user_id = ? OR m.subject_user_id = ? OR (m.scope = 'private' AND "
            "m.chat_id = ?))", [uid, uid, uid])


class PrivacyCommands:
    def __init__(self, db: Database, memory: Forgetter, *, owner_id: int | None,
                 timezone: str = "Asia/Yekaterinburg", clock: Clock = utcnow,
                 per_hour: int = 12) -> None:
        self._db = db
        self._memory = memory
        self._owner = owner_id
        self._tz = ZoneInfo(timezone)
        self._clock = clock
        self._per_hour = per_hour
        self._calls: dict[int, deque[datetime]] = {}
        self._warned: set[int] = set()

    @staticmethod
    def _parse(text: str) -> tuple[str, str]:
        name, _, arg = (text or "").strip().partition(" ")
        return name.split("@", 1)[0].lower(), arg.strip()

    def accepts(self, msg: IncomingMessage) -> bool:
        if not msg.is_private or msg.sender_id is None or msg.is_outgoing:
            return False
        name, _ = self._parse(msg.text)
        if name in OWNER_ONLY:
            return msg.sender_id == self._owner
        return name in NAMES

    def _rate_ok(self, uid: int) -> bool | None:
        """True — можно; False — предупредить один раз; None — молча пропустить."""
        if uid == self._owner:
            return True
        now = self._clock()
        q = self._calls.setdefault(uid, deque())
        while q and now - q[0] > timedelta(hours=1):
            q.popleft()
        if len(q) >= self._per_hour:
            if uid in self._warned:
                return None
            self._warned.add(uid)
            return False
        self._warned.discard(uid)
        q.append(now)
        return True

    async def handle(self, msg: IncomingMessage) -> list[str]:
        """Ответ (одно или несколько сообщений); [] — ничего не отправлять."""
        if not self.accepts(msg):
            return []
        uid = msg.sender_id
        assert uid is not None
        ok = self._rate_ok(uid)
        if ok is None:
            return []
        if ok is False:
            return ["слишком много команд подряд, давай через час"]
        name, arg = self._parse(msg.text)
        if name == "/privacy":
            return [HELP + (OWNER_HELP if uid == self._owner else "")]
        if name == "/mydata":
            return [await self._mydata(uid)]
        if name == "/export":
            return await self._export(uid)
        if name == "/forget":
            return [await self._forget_one(uid, arg)]
        if name == "/forget_me":
            return [await self._forget_me(uid, arg)]
        if name == "/forget_user":
            return [await self._forget_target(uid, "user", arg)]
        return [await self._forget_target(uid, "chat", arg)]

    # --- чтение --------------------------------------------------------------

    async def _rows(self, uid: int, limit: int | None = None) -> list[sqlite3.Row]:
        where, params = _about(uid)
        sql = (f"SELECT m.id, m.scope, m.chat_id, m.content, m.created_at, c.title "
               f"FROM memories m LEFT JOIN chats c ON c.chat_id = m.chat_id "
               f"WHERE {where} AND m.superseded_by IS NULL ORDER BY m.created_at DESC, m.seq DESC")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return await self._db.fetchall(sql, params)

    async def _counts(self, uid: int) -> tuple[int, int, int]:
        where, params = _about(uid)
        mem = await self._db.fetchall(f"SELECT COUNT(*) FROM memories m WHERE {where}", params)
        msgs = await self._db.fetchall(
            "SELECT COUNT(*) FROM messages WHERE sender_user_id = ? OR chat_id = ?", (uid, uid))
        rel = await self._db.fetchall(
            "SELECT COUNT(*) FROM relationships WHERE user_id = ?", (uid,))
        return int(mem[0][0]), int(msgs[0][0]), int(rel[0][0])

    def _line(self, r: sqlite3.Row) -> str:
        try:
            when = datetime.fromisoformat(str(r[4]).replace("Z", "+00:00")).astimezone(self._tz)
            date = f"{when:%d.%m.%y}"
        except ValueError:
            date = "?"
        where = _SCOPE.get(str(r[1]), str(r[1]))
        if r[1] == "chat" and r[5]:
            where = f"группа «{r[5]}»"
        content = " ".join(str(r[3]).split())
        return f"{str(r[0])[:8]} · {date} · {where} · {content}"

    async def _mydata(self, uid: int) -> str:
        mem, msgs, rel = await self._counts(uid)
        if mem == 0 and msgs == 0 and rel == 0:
            return "я о тебе ничего не храню"
        head = (f"записей о тебе: {mem}\nсообщений в истории: {msgs}\n"
                f"отношение ко мне: {'да' if rel else 'нет'}")
        rows = await self._rows(uid, limit=10)
        if not rows:
            return head
        tail = "\n\nпоследние записи:\n" + "\n".join(self._line(r) for r in rows)
        more = "\n\nвсё — /export · забыть одну — /forget <id> · всё — /forget_me да"
        return (head + tail)[:PART_LIMIT] + more

    async def _export(self, uid: int) -> list[str]:
        rows = await self._rows(uid)
        if not rows:
            return ["записей о тебе нет"]
        parts: list[str] = []
        cur = f"все записи о тебе ({len(rows)}):"
        shown = 0
        for r in rows:
            line = self._line(r)[:PART_LIMIT - 10]
            if len(cur) + len(line) + 1 > PART_LIMIT:
                parts.append(cur)
                if len(parts) >= MAX_PARTS:
                    break
                cur = line
            else:
                cur += "\n" + line
            shown += 1
        else:
            parts.append(cur)
        if shown < len(rows):
            parts.append(f"и ещё {len(rows) - shown} — слишком много для чата, "
                         f"попроси владельца выгрузить файлом")
        return parts

    # --- удаление ------------------------------------------------------------

    async def _forget_one(self, uid: int, arg: str) -> str:
        prefix = arg.strip().lower()
        if len(prefix) < MIN_PREFIX:
            return "формат: /forget <id> — id смотри в /mydata"
        where, params = _about(uid)
        rows = await self._db.fetchall(
            f"SELECT m.id, m.scope, m.chat_id FROM memories m WHERE {where} "
            f"AND lower(m.id) LIKE ? ESCAPE '\\'",
            [*params, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"])
        if not rows:
            return "такой записи о тебе нет"
        if len(rows) > 1:
            return "под это подходит несколько записей, дай id подлиннее"
        mid, scope, chat_id = str(rows[0][0]), str(rows[0][1]), rows[0][2]
        ctx = (MemoryContext.group(int(chat_id), uid) if scope == "chat"
               else MemoryContext.private(uid, int(chat_id)))
        report = await self._memory.forget_memory(ctx, mid, is_admin=uid == self._owner)
        if report is None:
            return "такой записи о тебе нет"
        return "забыла"

    async def _forget_me(self, uid: int, arg: str) -> str:
        if arg.strip().lower() not in YES:
            mem, msgs, _ = await self._counts(uid)
            return (f"я забуду всё о тебе: {mem} записей, {msgs} сообщений, напоминания и "
                    f"наше отношение. вернуть будет нельзя\nесли точно — напиши /forget_me да")
        report = await self._memory.forget_user(uid, requested_by=uid)
        log.info("privacy: пользователь удалил свои данные (записей %d)", report.memories)
        return "готово, я всё забыла. если напишешь снова — начнём с чистого листа"

    async def _forget_target(self, uid: int, kind: str, arg: str) -> str:
        target, _, confirm = arg.partition(" ")
        try:
            tid = int(target)
        except ValueError:
            return f"формат: /forget_{kind} <id> да"
        if confirm.strip().lower() not in YES:
            if kind == "user":
                mem, msgs, _ = await self._counts(tid)
            else:
                m = await self._db.fetchall(
                    "SELECT COUNT(*) FROM memories WHERE chat_id = ?", (tid,))
                s = await self._db.fetchall(
                    "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (tid,))
                mem, msgs = int(m[0][0]), int(s[0][0])
            return (f"удалю {mem} записей и {msgs} сообщений. "
                    f"подтверди: /forget_{kind} {tid} да")
        if kind == "user":
            report = await self._memory.forget_user(tid, requested_by=uid)
        else:
            report = await self._memory.forget_chat(tid, requested_by=uid)
        return f"удалено: записей {report.memories}, сообщений {report.messages}"
