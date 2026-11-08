"""Forget API (ADR-0010): удаление сразу из всех хранилищ + журнал удалений без содержимого.

Хранилища: memories, memories_fts (триггер), memory_embeddings и memory_links (каскад),
messages, outbox, личные чаты, Markdown-зеркало (обновляет MemoryService после коммита).
Те же функции удаления повторно применяются к БД, восстановленной из бэкапа.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from alya.memory.models import ScopeKey
from alya.memory.repository import scope_keys_where

type TargetType = Literal["user", "chat", "memory"]


@dataclass(frozen=True, slots=True)
class ForgetReport:
    target_type: TargetType
    target_id: str
    memories: int
    messages: int
    affected: list[ScopeKey] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Tombstone:
    target_type: TargetType
    target_id: str
    created_at: str


def _marks(n: int) -> str:
    return ",".join("?" * n)


def delete_memory_sync(conn: sqlite3.Connection, memory_id: str) -> ForgetReport:
    keys = scope_keys_where(conn, "m.id = ?", [memory_id])
    n = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount
    return ForgetReport("memory", memory_id, n, 0, keys)


def delete_user_sync(conn: sqlite3.Connection, user_id: int) -> ForgetReport:
    private = [
        int(r[0]) for r in conn.execute(
            "SELECT chat_id FROM chats WHERE kind = 'private' AND (chat_id = ? OR chat_id IN "
            "(SELECT chat_id FROM chat_members WHERE user_id = ?))",
            (user_id, user_id),
        ).fetchall()
    ]
    pm = _marks(len(private))
    chat_cond = f" OR m.chat_id IN ({pm})" if private else ""
    mem_where = f"(m.owner_user_id = ? OR m.subject_user_id = ?{chat_cond})"
    mem_params = [user_id, user_id, *private]
    keys = scope_keys_where(conn, mem_where, mem_params)
    memories = conn.execute(
        f"SELECT COUNT(*) FROM memories m WHERE {mem_where}", mem_params).fetchone()[0]
    msg_where = "sender_user_id = ?" + (f" OR chat_id IN ({pm})" if private else "")
    messages = conn.execute(f"DELETE FROM messages WHERE {msg_where}",
                            [user_id, *private]).rowcount
    if private:
        conn.execute(f"DELETE FROM outbox WHERE chat_id IN ({pm})", private)
        conn.execute(f"DELETE FROM chats WHERE chat_id IN ({pm})", private)
    conn.execute(f"DELETE FROM memories AS m WHERE {mem_where}", mem_params)
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    return ForgetReport("user", str(user_id), int(memories), messages, keys)


def delete_chat_sync(conn: sqlite3.Connection, chat_id: int) -> ForgetReport:
    keys = scope_keys_where(conn, "m.chat_id = ?", [chat_id])
    memories = conn.execute("SELECT COUNT(*) FROM memories WHERE chat_id = ?",
                            (chat_id,)).fetchone()[0]
    messages = conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,)).rowcount
    conn.execute("DELETE FROM outbox WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM memories WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    return ForgetReport("chat", str(chat_id), int(memories), messages, keys)


def record_tombstone(
    conn: sqlite3.Connection, target_type: TargetType, target_id: str,
    requested_by: int | None, now_iso: str,
) -> None:
    conn.execute(
        "INSERT INTO deletion_journal (target_type, target_id, requested_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (target_type, target_id, requested_by, now_iso),
    )


def list_tombstones(conn: sqlite3.Connection, since: str | None = None) -> list[Tombstone]:
    sql = "SELECT target_type, target_id, created_at FROM deletion_journal"
    params: list[str] = []
    if since is not None:
        sql += " WHERE created_at > ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [Tombstone(r[0], str(r[1]), r[2]) for r in rows
            if r[0] in ("user", "chat", "memory")]


def apply_tombstone(conn: sqlite3.Connection, t: Tombstone) -> ForgetReport:
    if t.target_type == "user":
        return delete_user_sync(conn, int(t.target_id))
    if t.target_type == "chat":
        return delete_chat_sync(conn, int(t.target_id))
    return delete_memory_sync(conn, t.target_id)


def replay_tombstones(conn: sqlite3.Connection, tombstones: Sequence[Tombstone]) -> int:
    """После restore: повторно удалить всё из журнала. Вызывать внутри транзакции."""
    known = {(r[0], str(r[1])) for r in conn.execute(
        "SELECT target_type, target_id FROM deletion_journal").fetchall()}
    removed = 0
    for t in tombstones:
        report = apply_tombstone(conn, t)
        removed += report.memories + report.messages
        if (t.target_type, t.target_id) not in known:
            record_tombstone(conn, t.target_type, t.target_id, None, t.created_at)
            known.add((t.target_type, t.target_id))
    return removed
