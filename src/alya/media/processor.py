"""Превращает вложения входящих сообщений в текст для персонажа.

Вызывается только для сообщений, адресованных Але: чужие фото и голосовые в группах не
скачиваются и не стоят денег. Любая ошибка (лимит, размер, сеть, модель) даёт
честную пометку вместо описания — ответ не ломается.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from alya.media import audio
from alya.media.limits import UsageLimiter
from alya.media.models import MediaRef

log = logging.getLogger(__name__)

# (file_id, max_bytes) -> содержимое файла
type Downloader = Callable[[int, int], Awaitable[bytes]]

_LABELS = {
    "photo": "фото", "video": "видео", "animation": "гифка", "document": "файл",
    "audio": "музыка", "voice": "голосовое", "video_note": "кружок", "sticker": "стикер",
}


class Vision(Protocol):
    async def describe(self, data: bytes, *, caption: str = "") -> str: ...


class SpeechToText(Protocol):
    async def transcribe(self, data: bytes, fmt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ProcessorLimits:
    max_image_bytes: int = 10 * 1024 * 1024
    max_voice_seconds: int = 180
    max_voice_bytes: int = 20 * 1024 * 1024


_DEFAULT_LIMITS = ProcessorLimits()


def _with_caption(label: str, caption: str) -> str:
    return f"{label} {caption}".strip() if caption else label


class MediaProcessor:
    def __init__(self, limiter: UsageLimiter, *, vision: Vision | None = None,
                 stt: SpeechToText | None = None, limits: ProcessorLimits = _DEFAULT_LIMITS,
                 has_ffmpeg: Callable[[], bool] = lambda: audio.ffmpeg_path() is not None) -> None:
        self._limiter = limiter
        self._vision = vision
        self._stt = stt
        self._limits = limits
        self._has_ffmpeg = has_ffmpeg

    async def describe(self, media: MediaRef, caption: str, download: Downloader, *,
                       chat_id: int | None = None) -> str:
        """Текст вместо пометки вида «[фото]». caption — подпись без пометки."""
        label = _LABELS.get(media.kind, "вложение")
        if media.kind == "sticker":
            return f"[стикер {media.emoji}]" if media.emoji else "[стикер]"
        if media.is_image:
            return await self._image(media, caption, download, chat_id)
        if media.is_speech:
            return await self._speech(media, label, caption, download, chat_id)
        return _with_caption(f"[{label}]", caption)

    async def _image(self, media: MediaRef, caption: str, download: Downloader,
                     chat_id: int | None) -> str:
        plain = _with_caption("[фото]", caption)
        if self._vision is None or media.file_id is None:
            return plain
        if media.size > self._limits.max_image_bytes:
            return _with_caption("[фото — слишком большое, не рассмотрела]", caption)
        if not await self._limiter.take("vision", chat_id):
            return plain
        try:
            data = await download(media.file_id, self._limits.max_image_bytes)
            desc = await self._vision.describe(data, caption=caption)
        except Exception as exc:
            log.warning("не удалось рассмотреть фото: %s", type(exc).__name__)
            return plain
        return _with_caption(f"[фото: {desc}]", caption)

    async def _speech(self, media: MediaRef, label: str, caption: str, download: Downloader,
                      chat_id: int | None) -> str:
        plain = _with_caption(f"[{label}]", caption)
        if self._stt is None or media.file_id is None:
            return plain
        if (media.duration_s > self._limits.max_voice_seconds
                or media.size > self._limits.max_voice_bytes):
            return f"[{label} на {media.duration_s} с — слишком длинное, не прослушала]"
        if not self._has_ffmpeg():
            return plain
        if not await self._limiter.take("stt", chat_id):
            return plain
        try:
            data = await download(media.file_id, self._limits.max_voice_bytes)
            mp3 = await audio.speech_to_mp3(data, max_seconds=self._limits.max_voice_seconds)
            text = " ".join((await self._stt.transcribe(mp3, "mp3")).split())
        except Exception as exc:
            log.warning("не удалось распознать %s: %s", label, type(exc).__name__)
            return plain
        if not text:
            return f"[{label} — не разобрала слов]"
        return _with_caption(f"[{label}] {text}", caption)
