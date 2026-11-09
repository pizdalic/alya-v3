"""Очистка текста: управляющие символы, bidi-подмены, лимиты Telegram."""

from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
INCOMING_LIMIT = 8000
# Управляющие символы (кроме \t и \n), zero-width space, LRM/RLM и bidi-override.
# ZWJ/ZWNJ (\u200c, \u200d) сохраняются — на них держатся составные эмодзи.
_BAD = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACES = re.compile(r"[ \t]+")


# Модель иногда пишет служебные метки медиа текстом («[стикер 🐱]», «стикер: LINE_White_Cat_Girl»).
# Стикер шлётся отдельным полем плана, а в чат такие метки попадать не должны.
_MARKER_INLINE = re.compile(
    r"\[(?:стикер|sticker|фото|голосовое|кружок|гиф|gif|видео)[^\]\n]*\]", re.IGNORECASE
)
_MARKER_LINE = re.compile(r"^(?:стикер|sticker)\s*[:：]\s*\S+$", re.IGNORECASE)
# Ролевые пометки в звёздочках: «*фото*», «*стикер: LINE_White_Cat_Girl*», «*отправляет селфи*».
_MARKER_STARS = re.compile(
    r"\*{1,2}[^*\n]{0,40}?(?:стикер|sticker|фото|фотк|селфи|selfie|голосов|кружок|гиф|gif|видео)"
    r"[^*\n]{0,80}\*{1,2}", re.IGNORECASE)


def strip_media_markers(text: str) -> str:
    text = _MARKER_STARS.sub("", _MARKER_INLINE.sub("", text))
    return "\n".join(line for line in text.split("\n") if not _MARKER_LINE.match(line.strip()))


def clean_incoming(text: str | None) -> str:
    if not text:
        return ""
    text = _BAD.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text[:INCOMING_LIMIT].strip()


def clean_outgoing(text: str) -> list[str]:
    """Готовый к отправке текст; длинный режется по переносам и пробелам."""
    text = strip_media_markers(text)
    text = _BAD.sub("", text.replace("\r", ""))
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n")).strip()
    parts: list[str] = []
    while len(text) > TELEGRAM_LIMIT:
        cut = text.rfind("\n", 0, TELEGRAM_LIMIT)
        if cut < TELEGRAM_LIMIT // 2:
            cut = text.rfind(" ", 0, TELEGRAM_LIMIT)
        if cut < TELEGRAM_LIMIT // 2:
            cut = TELEGRAM_LIMIT
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return [p for p in parts if p]
