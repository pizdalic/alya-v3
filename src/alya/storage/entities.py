"""Пользователи, чаты, участники: идемпотентные upsert-операции (вызываются внутри транзакции)."""

from __future__ import annotations

import sqlite3
from typing import Literal

type ChatType = Literal["private", "group", "supergroup", "channel"]


def upsert_user(
    conn: sqlite3.Connection,
    user_id: int,
    now: str,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO users (user_id, username, display_name, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            username = COALESCE(excluded.username, users.username),
            display_name = COALESCE(excluded.display_name, users.display_name),
            last_seen_at = excluded.last_seen_at
        """,
        (user_id, username, display_name, now, now),
    )


def upsert_chat(
    conn: sqlite3.Connection, chat_id: int, kind: ChatType, now: str, *, title: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO chats (chat_id, kind, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (chat_id) DO UPDATE SET
            title = COALESCE(excluded.title, chats.title),
            updated_at = excluded.updated_at
        """,
        (chat_id, kind, title, now, now),
    )


def add_member(conn: sqlite3.Connection, chat_id: int, user_id: int, now: str) -> None:
    conn.execute(
        """
        INSERT INTO chat_members (chat_id, user_id, updated_at) VALUES (?, ?, ?)
        ON CONFLICT (chat_id, user_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (chat_id, user_id, now),
    )
