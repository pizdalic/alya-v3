"""Репозиторий памяти. Любое чтение/изменение записи пользователя идёт через MemoryContext.

Невидимая в контексте запись неотличима от несуществующей (get → None), чтобы не было оракула.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from alya.core.ids import new_ulid
from alya.core.timeutil import Clock, iso, utcnow
from alya.memory.models import LAYERS, Layer, Memory, MemoryContext, MemoryScopeError, ScopeKey
from alya.memory.scope import scoped_where
from alya.storage.db import Database

MAX_CONTENT = 4000
MAX_PROVENANCE = 2000
_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

COLUMNS = (
    "m.id, m.scope, m.owner_user_id, m.chat_id, m.subject_user_id, m.layer, m.category, "
    "m.content, m.provenance, m.confidence, m.is_permanent, m.usage_count, m.last_used_at, "
    "m.superseded_by, m.created_at, m.updated_at, m.expires_at"
)


@dataclass(frozen=True, slots=True)
class LayerPolicy:
    working_ttl: timedelta = timedelta(hours=6)
    short_ttl: timedelta = timedelta(days=14)

    def expires_at(self, layer: Layer, now: datetime, permanent: bool) -> str | None:
        if permanent or layer == "long":
            return None
        return iso(now + (self.working_ttl if layer == "working" else self.short_ttl))


def row_to_memory(row: sqlite3.Row | Sequence[Any]) -> Memory:
    return Memory(
        id=row[0], scope=row[1], owner_user_id=row[2], chat_id=row[3], subject_user_id=row[4],
        layer=row[5], category=row[6], content=row[7], provenance=json.loads(row[8]),
        confidence=float(row[9]), is_permanent=bool(row[10]), usage_count=int(row[11]),
        last_used_at=row[12], superseded_by=row[13], created_at=row[14], updated_at=row[15],
        expires_at=row[16],
    )


def _clean_content(content: str) -> str:
    text = content.strip()
    if not text:
        raise ValueError("пустое содержимое памяти")
    if len(text) > MAX_CONTENT:
        raise ValueError(f"содержимое длиннее {MAX_CONTENT} символов")
    return text


def _clean_provenance(provenance: dict[str, Any] | None) -> str:
    raw = json.dumps(provenance or {}, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > MAX_PROVENANCE:
        raise ValueError("provenance слишком большой")
    return raw


def _check(layer: Layer, category: str, confidence: float) -> None:
    if layer not in LAYERS:
        raise ValueError(f"неизвестный уровень памяти: {layer}")
    if not _CATEGORY.match(category):
        raise ValueError("категория: латиница в нижнем регистре, цифры и _, до 32 символов")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence должен быть в [0, 1]")


def fetch_scope(conn: sqlite3.Connection, key: ScopeKey, now_iso: str) -> list[Memory]:
    """Все живые записи одной области — для зеркала (системный уровень, не для ответов)."""
    sql = f"SELECT {COLUMNS} FROM memories m WHERE m.scope = ?"
    params: list[Any] = [key.scope]
    if key.scope != "character":
        sql += " AND m.chat_id = ?"
        params.append(key.chat_id)
    if key.scope == "private":
        sql += " AND m.owner_user_id = ?"
        params.append(key.owner_user_id)
    sql += (" AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)"
            " ORDER BY m.layer, m.created_at")
    params.append(now_iso)
    return [row_to_memory(r) for r in conn.execute(sql, params).fetchall()]


def scope_keys_where(conn: sqlite3.Connection, where: str, params: Sequence[Any]) -> list[ScopeKey]:
    rows = conn.execute(
        f"SELECT DISTINCT m.scope, m.chat_id, m.owner_user_id FROM memories m WHERE {where}",
        list(params),
    ).fetchall()
    return [ScopeKey(r[0], r[1], r[2]) for r in rows]


class MemoryRepository:
    def __init__(self, db: Database, policy: LayerPolicy | None = None,
                 clock: Clock = utcnow) -> None:
        self._db = db
        self._policy = policy or LayerPolicy()
        self._clock = clock

    @property
    def db(self) -> Database:
        return self._db

    def now_iso(self) -> str:
        return iso(self._clock())

    async def add(
        self,
        ctx: MemoryContext,
        content: str,
        *,
        scope: Literal["private", "chat"],
        layer: Layer = "long",
        category: str = "fact",
        subject_user_id: int | None = None,
        provenance: dict[str, Any] | None = None,
        confidence: float = 0.7,
        is_permanent: bool = False,
    ) -> Memory:
        _check(layer, category, confidence)
        if scope == "private":
            if ctx.chat_kind != "private":
                raise MemoryScopeError("личную память можно записать только в личке")
            if subject_user_id not in (None, ctx.user_id):
                raise MemoryScopeError("личная память может быть только о владельце")
            owner, chat, subject = ctx.user_id, ctx.chat_id, ctx.user_id
        elif scope == "chat":
            if ctx.chat_kind != "group":
                raise MemoryScopeError("память чата можно записать только в группе")
            owner, chat, subject = None, ctx.chat_id, subject_user_id
        else:
            raise MemoryScopeError(f"недопустимая область: {scope}")
        return await self._insert(scope, owner, chat, subject, layer, category, content,
                                  provenance, confidence, is_permanent)

    async def add_character(
        self,
        content: str,
        *,
        layer: Layer = "long",
        category: str = "self",
        provenance: dict[str, Any] | None = None,
        confidence: float = 0.9,
        is_permanent: bool = False,
    ) -> Memory:
        """Факты о самой Але. Вызывающий отвечает за отсутствие данных пользователей."""
        _check(layer, category, confidence)
        return await self._insert("character", None, None, None, layer, category, content,
                                  provenance, confidence, is_permanent)

    async def _insert(
        self, scope: str, owner: int | None, chat: int | None, subject: int | None,
        layer: Layer, category: str, content: str, provenance: dict[str, Any] | None,
        confidence: float, is_permanent: bool,
    ) -> Memory:
        text = _clean_content(content)
        prov = _clean_provenance(provenance)
        now = self._clock()
        now_s = iso(now)
        memory_id = new_ulid()
        expires = self._policy.expires_at(layer, now, is_permanent)

        def op(conn: sqlite3.Connection) -> Memory:
            conn.execute(
                """
                INSERT INTO memories (id, scope, owner_user_id, chat_id, subject_user_id, layer,
                    category, content, provenance, confidence, is_permanent, created_at,
                    updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (memory_id, scope, owner, chat, subject, layer, category, text, prov,
                 confidence, int(is_permanent), now_s, now_s, expires),
            )
            row = conn.execute(f"SELECT {COLUMNS} FROM memories m WHERE m.id = ?",
                               (memory_id,)).fetchone()
            return row_to_memory(row)

        return await self._db.transaction(op)

    async def get(self, ctx: MemoryContext, memory_id: str) -> Memory | None:
        found = await self.get_many(ctx, [memory_id])
        return found[0] if found else None

    async def get_many(self, ctx: MemoryContext, ids: Sequence[str]) -> list[Memory]:
        if not ids:
            return []
        where, params = scoped_where(ctx, self.now_iso())
        marks = ",".join("?" * len(ids))

        def op(conn: sqlite3.Connection) -> list[Memory]:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM memories m WHERE m.id IN ({marks}) AND {where}",
                [*ids, *params],
            ).fetchall()
            return [row_to_memory(r) for r in rows]

        return await self._db.read(op)

    async def list(
        self, ctx: MemoryContext, *, layer: Layer | None = None, limit: int = 100
    ) -> list[Memory]:
        where, params = scoped_where(ctx, self.now_iso())
        if layer is not None:
            where += " AND m.layer = ?"
            params.append(layer)

        def op(conn: sqlite3.Connection) -> list[Memory]:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM memories m WHERE {where} "
                "ORDER BY m.created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [row_to_memory(r) for r in rows]

        return await self._db.read(op)

    async def update_content(
        self, ctx: MemoryContext, memory_id: str, content: str, *, confidence: float | None = None
    ) -> Memory | None:
        """Изменить текст. Старые эмбеддинги удаляются в той же транзакции (FTS — триггером)."""
        text = _clean_content(content)
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence должен быть в [0, 1]")
        now_s = self.now_iso()
        where, params = scoped_where(ctx, now_s)

        def op(conn: sqlite3.Connection) -> Memory | None:
            row = conn.execute(f"SELECT m.id FROM memories m WHERE m.id = ? AND {where}",
                               [memory_id, *params]).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE memories SET content = ?, confidence = COALESCE(?, confidence), "
                "updated_at = ? WHERE id = ?",
                (text, confidence, now_s, memory_id),
            )
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
            new = conn.execute(f"SELECT {COLUMNS} FROM memories m WHERE m.id = ?",
                               (memory_id,)).fetchone()
            return row_to_memory(new)

        return await self._db.transaction(op)

    async def supersede(self, ctx: MemoryContext, old_id: str, new_id: str) -> bool:
        """Пометить old заменённой new. Обе записи должны быть видимы и из одной области."""
        if old_id == new_id:
            return False
        now_s = self.now_iso()
        where, params = scoped_where(ctx, now_s)

        def op(conn: sqlite3.Connection) -> bool:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM memories m WHERE m.id IN (?, ?) AND {where}",
                [old_id, new_id, *params],
            ).fetchall()
            if len(rows) != 2:
                return False
            a, b = (row_to_memory(r) for r in rows)
            if a.scope_key != b.scope_key:
                return False
            conn.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                         (new_id, now_s, old_id))
            conn.execute(
                "INSERT OR IGNORE INTO memory_links (from_id, to_id, relation, created_at) "
                "VALUES (?, ?, 'derived_from', ?)",
                (new_id, old_id, now_s),
            )
            return True

        return await self._db.transaction(op)

    async def touch(self, ids: Iterable[str]) -> None:
        id_list = list(dict.fromkeys(ids))
        if not id_list:
            return
        now_s = self.now_iso()
        marks = ",".join("?" * len(id_list))

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"UPDATE memories SET usage_count = usage_count + 1, last_used_at = ? "
                f"WHERE id IN ({marks})",
                [now_s, *id_list],
            )

        await self._db.transaction(op)

    async def fetch_scope(self, key: ScopeKey) -> list[Memory]:
        now_s = self.now_iso()
        return await self._db.read(lambda conn: fetch_scope(conn, key, now_s))

    async def purge_expired(self) -> tuple[int, int, list[ScopeKey]]:
        """Удалить истёкшие записи памяти и сообщения с TTL. → (память, сообщения, области)."""
        now_s = self.now_iso()

        def op(conn: sqlite3.Connection) -> tuple[int, int, list[ScopeKey]]:
            cond = "m.expires_at IS NOT NULL AND m.expires_at <= ? AND m.is_permanent = 0"
            keys = scope_keys_where(conn, cond, [now_s])
            mem = conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ? "
                "AND is_permanent = 0",
                (now_s,),
            ).rowcount
            msg = conn.execute(
                "DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_s,)
            ).rowcount
            return mem, msg, keys

        return await self._db.transaction(op)
