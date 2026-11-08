"""Очистка текста: управляющие символы, bidi-подмены, лимиты Telegram."""

from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
INCOMING_LIMIT = 8000
# Управляющие символы (кроме \t и \n), zero-width space, LRM/RLM и bidi-override.
# ZWJ/ZWNJ (\u200c, \u200d) сохраняются — на них держатся составные эмодзи.
_BAD = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACES = re.compile(r"[ \t]+")


def clean_incoming(text: str | None) -> str:
    if not text:
        return ""
    text = _BAD.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text[:INCOMING_LIMIT].strip()


def clean_outgoing(text: str) -> list[str]:
    """Готовый к отправке текст; длинный режется по переносам и пробелам."""
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
