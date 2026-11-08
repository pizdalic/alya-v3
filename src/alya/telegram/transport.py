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
