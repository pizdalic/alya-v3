"""Фасад памяти: запись, поиск, Forget и синхронизация зеркала после каждого коммита."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from typing import Any, Literal

from alya.core.timeutil import iso, utcnow
from alya.memory.forget import (
    ForgetReport,
    delete_chat_sync,
    delete_memory_sync,
    delete_user_sync,
    record_tombstone,
)
from alya.memory.mirror import MarkdownMirror
from alya.memory.models import Layer, Memory, MemoryContext, MemoryScopeError, ScopeKey
from alya.memory.repository import MemoryRepository, fetch_scope
from alya.memory.search import MemorySearch, SearchResult

log = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, repo: MemoryRepository, search: MemorySearch,
                 mirror: MarkdownMirror | None = None) -> None:
        self._repo = repo
        self._search = search
        self._mirror = mirror

    @property
    def repo(self) -> MemoryRepository:
        return self._repo

    async def remember(
        self, ctx: MemoryContext, content: str, *, scope: Literal["private", "chat"],
        layer: Layer = "long", category: str = "fact", subject_user_id: int | None = None,
        provenance: dict[str, Any] | None = None, confidence: float = 0.7,
        is_permanent: bool = False,
    ) -> Memory:
        memory = await self._repo.add(
            ctx, content, scope=scope, layer=layer, category=category,
            subject_user_id=subject_user_id, provenance=provenance, confidence=confidence,
            is_permanent=is_permanent)
        await self.refresh_mirror([memory.scope_key])
        return memory

    async def remember_character(self, content: str, **kwargs: Any) -> Memory:
        memory = await self._repo.add_character(content, **kwargs)
        await self.refresh_mirror([memory.scope_key])
        return memory

    async def recall(self, ctx: MemoryContext, query: str, *, k: int = 8) -> SearchResult:
        return await self._search.search(ctx, query, k=k)

    async def update(self, ctx: MemoryContext, memory_id: str, content: str) -> Memory | None:
        memory = await self._repo.update_content(ctx, memory_id, content)
        if memory is not None:
            await self.refresh_mirror([memory.scope_key])
        return memory

    async def forget_memory(
        self, ctx: MemoryContext, memory_id: str, *, is_admin: bool = False
    ) -> ForgetReport | None:
        """Пользователь может забыть свою личную запись или групповую запись о себе.
        Записи персонажа и чужие групповые — только администратор."""
        memory = await self._repo.get(ctx, memory_id)
        if memory is None:
            return None
        allowed = is_admin or (
            (memory.scope == "private" and memory.owner_user_id == ctx.user_id)
            or (memory.scope == "chat" and memory.subject_user_id == ctx.user_id)
        )
        if not allowed:
            raise MemoryScopeError("нет прав удалить эту запись")
        return await self._forget(lambda c: delete_memory_sync(c, memory_id), "memory",
                                  memory_id, ctx.user_id)

    async def forget_user(self, user_id: int, *, requested_by: int | None) -> ForgetReport:
        report = await self._forget(lambda c: delete_user_sync(c, user_id), "user",
                                    str(user_id), requested_by)
        if self._mirror is not None:
            await asyncio.to_thread(self._mirror.remove_user, user_id)
        return report

    async def forget_chat(self, chat_id: int, *, requested_by: int | None) -> ForgetReport:
        return await self._forget(lambda c: delete_chat_sync(c, chat_id), "chat",
                                  str(chat_id), requested_by)

    async def _forget(
        self, fn: Any, target_type: Literal["user", "chat", "memory"], target_id: str,
        requested_by: int | None,
    ) -> ForgetReport:
        now_s = iso(utcnow())

        def op(conn: sqlite3.Connection) -> ForgetReport:
            report: ForgetReport = fn(conn)
            record_tombstone(conn, target_type, target_id, requested_by, now_s)
            return report

        report = await self._repo.db.transaction(op)
        await self.refresh_mirror(report.affected)
        log.info("forget %s: memories=%d messages=%d", target_type, report.memories,
                 report.messages)
        return report

    async def purge_expired(self) -> tuple[int, int]:
        memories, messages, keys = await self._repo.purge_expired()
        await self.refresh_mirror(keys)
        return memories, messages

    async def refresh_mirror(self, keys: Iterable[ScopeKey]) -> None:
        if self._mirror is None:
            return
        mirror = self._mirror
        unique = sorted(set(keys), key=ScopeKey.sort_key)
        for key in unique:
            memories = await self._repo.fetch_scope(key)
            await asyncio.to_thread(mirror.write, key, memories)

    async def rebuild_mirror(self) -> int:
        """Полная пересборка зеркала из БД (например, после restore)."""
        if self._mirror is None:
            return 0
        mirror = self._mirror
        now_s = self._repo.now_iso()

        def collect(conn: sqlite3.Connection) -> dict[ScopeKey, list[Memory]]:
            rows = conn.execute(
                "SELECT DISTINCT scope, chat_id, owner_user_id FROM memories").fetchall()
            return {ScopeKey(r[0], r[1], r[2]): fetch_scope(conn, ScopeKey(r[0], r[1], r[2]),
                                                             now_s) for r in rows}

        groups = await self._repo.db.read(collect)

        def write_all() -> int:
            for path in mirror.files():
                path.unlink()
            return sum(mirror.write(k, v) is not None for k, v in groups.items())

        return await asyncio.to_thread(write_all)
