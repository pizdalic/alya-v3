"""Единственное место, где строится SQL-условие видимости.
Все запросы памяти обязаны его использовать."""

from __future__ import annotations

from typing import Any

from alya.memory.models import MemoryContext


def visibility_clause(ctx: MemoryContext, alias: str = "m") -> tuple[str, list[Any]]:
    """character — везде; chat — только в этом чате; private — только в личке владельца."""
    a = alias
    sql = f"({a}.scope = 'character' OR ({a}.scope = 'chat' AND {a}.chat_id = ?)"
    params: list[Any] = [ctx.chat_id]
    if ctx.chat_kind == "private":
        sql += f" OR ({a}.scope = 'private' AND {a}.chat_id = ? AND {a}.owner_user_id = ?)"
        params += [ctx.chat_id, ctx.user_id]
    return sql + ")", params


def alive_clause(now_iso: str, alias: str = "m") -> tuple[str, list[Any]]:
    """Не заменённые и не истёкшие записи."""
    a = alias
    return (
        f"({a}.superseded_by IS NULL AND ({a}.expires_at IS NULL OR {a}.expires_at > ?))",
        [now_iso],
    )


def scoped_where(ctx: MemoryContext, now_iso: str, alias: str = "m") -> tuple[str, list[Any]]:
    vis, vp = visibility_clause(ctx, alias)
    alive, ap = alive_clause(now_iso, alias)
    return f"{vis} AND {alive}", vp + ap
