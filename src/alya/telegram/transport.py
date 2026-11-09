"""Контракт транспорта: то, что слою отправки нужно от Telegram."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from alya.core.errors import AlyaError


class TransportError(AlyaError):
    """Ошибка отправки. permanent — повторять бессмысленно (нет доступа, чат удалён)."""

    def __init__(self, message: str, *, permanent: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class FloodWait(TransportError):
    """Telegram просит подождать: отправка ставится на паузу целиком."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"FloodWait {seconds:g} с", retry_after=seconds)
        self.seconds = seconds


class Transport(Protocol):
    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        """Отправить текст; вернуть окончательный id сообщения."""
        ...

    async def send_typing(self, chat_id: int) -> None: ...

    async def mark_read(self, chat_id: int, message_ids: Sequence[int]) -> None: ...


class MediaTransport(Transport, Protocol):
    """Отправка медиа и действия аккаунта (Phase 6). Пути — локальные файлы."""

    async def send_voice(self, chat_id: int, path: str, duration_s: int, *,
                         reply_to: int | None = None) -> int: ...

    async def send_photo(self, chat_id: int, path: str, *, caption: str = "",
                         reply_to: int | None = None) -> int: ...

    async def send_sticker(self, chat_id: int, remote_id: str, *,
                           reply_to: int | None = None) -> int: ...

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def download(self, file_id: int, max_bytes: int) -> bytes: ...

    async def join_by_link(self, link: str) -> int: ...

    async def set_bio(self, text: str) -> None: ...

    async def set_avatar(self, path: str) -> None: ...

    async def sticker_set(self, name: str) -> list[tuple[str, str]]: ...
