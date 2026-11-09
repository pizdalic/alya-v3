"""Модели медиа: входящие вложения и действия Али помимо текста."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type MediaKind = Literal["photo", "video", "animation", "document", "audio", "voice",
                         "video_note", "sticker"]


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Вложение входящего сообщения. file_id — локальный id файла TDLib."""

    kind: MediaKind
    file_id: int | None = None
    size: int = 0
    mime: str = ""
    duration_s: int = 0
    emoji: str = ""
    set_id: int | None = None

    @property
    def is_image(self) -> bool:
        return self.kind == "photo" or (self.kind == "document" and self.mime.startswith("image/"))

    @property
    def is_speech(self) -> bool:
        return self.kind in ("voice", "video_note")


@dataclass(frozen=True, slots=True)
class ReplyActions:
    """Что Аля делает помимо текста. Пустые поля — действия нет."""

    reaction: str = ""   # эмодзи-реакция на последнее сообщение
    sticker: str = ""    # эмодзи, по которому выбирается стикер
    voice: bool = False  # ответить голосовым вместо текста
    selfie: str = ""     # сцена для фото «себя»
    sticker_id: str = ""  # remote_id, уже выбранный до ответа (пусто — выбрать по эмодзи)

    @property
    def empty(self) -> bool:
        return not (self.reaction or self.sticker or self.voice or self.selfie)
