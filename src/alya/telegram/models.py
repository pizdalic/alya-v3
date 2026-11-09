"""Транспортно-независимые модели входящих событий Telegram."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alya.media.models import MediaRef

type ChatKind = Literal["private", "group", "supergroup", "channel"]


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    sender_id: int | None  # None — анонимный админ или канал
    chat_kind: ChatKind
    text: str
    date: datetime
    sender_name: str = ""
    sender_username: str | None = None
    is_outgoing: bool = False
    reply_to_message_id: int | None = None
    reply_to_user_id: int | None = None
    mentioned_user_ids: frozenset[int] = frozenset()
    mentioned_usernames: frozenset[str] = frozenset()  # без @, в нижнем регистре
    media_kind: str | None = None
    chat_title: str | None = None
    sender_is_contact: bool = False
    media: MediaRef | None = None  # вложение для скачивания (фото, голос, стикер)
    caption: str = ""  # подпись к вложению без пометки

    @property
    def is_private(self) -> bool:
        return self.chat_kind == "private"

    @property
    def is_group(self) -> bool:
        return self.chat_kind in ("group", "supergroup")

    @property
    def display_name(self) -> str:
        if self.sender_name:
            return self.sender_name
        if self.sender_username:
            return "@" + self.sender_username
        return str(self.sender_id)


@dataclass(frozen=True, slots=True)
class MessageEdited:
    chat_id: int
    message_id: int
    text: str
    edited_at: datetime


@dataclass(frozen=True, slots=True)
class MessagesDeleted:
    chat_id: int
    message_ids: tuple[int, ...]
