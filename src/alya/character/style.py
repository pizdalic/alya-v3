"""Пост-обработка ответа под манеру Али: маленькая буква, без точки, разбивка на сообщения,
редкие опечатки, эмодзи и скобочки «)» по бюджету «не больше N на 100 сообщений»
(окно хранится в БД)."""

from __future__ import annotations

import json
import random
import re
import sqlite3

from alya.character.persona import StyleProfile
from alya.core.timeutil import Clock, iso, utcnow
from alya.storage.db import Database

WINDOW = 100
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\uFE0F\u200D]+")
_URLISH = re.compile(r"https?://|www\.|@\w|\d")
# соседние клавиши ЙЦУКЕН — правдоподобные опечатки
_NEIGHBORS = {
    "а": "вп", "о": "рл", "е": "кн", "и": "мт", "р": "ок", "т": "иь", "н": "ег",
    "с": "чм", "л": "до", "в": "ыа", "к": "уе", "м": "си", "п": "ар", "д": "лж",
}


def _lower_first(text: str) -> str:
    for i, ch in enumerate(text):
        if ch.isalpha():
            nxt = text[i + 1 : i + 2]
            # не трогаем аббревиатуры вроде «ИИ», «США»
            if nxt.isupper():
                return text
            return text[:i] + ch.lower() + text[i + 1 :]
    return text


def _drop_period(text: str) -> str:
    if text.endswith(".") and not text.endswith(".."):
        return text[:-1].rstrip()
    return text


def split_messages(text: str, max_messages: int) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text.strip()) if p.strip()]
    if len(parts) <= max_messages:
        return parts
    head, tail = parts[: max_messages - 1], parts[max_messages - 1 :]
    return [*head, " ".join(tail)]


def make_typo(text: str, rng: random.Random) -> str:
    """Одна правдоподобная опечатка в длинном слове; ссылки, упоминания и числа не трогаем."""
    words = [(m.start(), m.group()) for m in re.finditer(r"[а-яё]{5,}", text)]
    words = [(pos, w) for pos, w in words if not _URLISH.search(w)]
    if not words:
        return text
    pos, word = rng.choice(words)
    i = rng.randrange(1, len(word) - 1)
    kind = rng.random()
    if kind < 0.5 and word[i] in _NEIGHBORS:
        new = word[:i] + rng.choice(_NEIGHBORS[word[i]]) + word[i + 1 :]
    elif kind < 0.8:
        new = word[:i] + word[i + 1] + word[i] + word[i + 2 :]  # перестановка
    else:
        new = word[:i] + word[i + 1 :]  # пропуск буквы
    return text[:pos] + new + text[pos + len(word) :]


def count_emoji(text: str) -> int:
    return len(_EMOJI.findall(text))


def strip_emoji(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text)).strip()


def count_smiley(text: str) -> int:
    """Скобочки-смайлики: «)» без парной «(»."""
    depth = n = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth:
                depth -= 1
            else:
                n += 1
    return n


def strip_smiley(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if not depth:
                continue
            depth -= 1
        out.append(ch)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


class Stylizer:
    def __init__(self, db: Database, style: StyleProfile, *, rng: random.Random | None = None,
                 clock: Clock = utcnow) -> None:
        self._db = db
        self._style = style
        self._rng = rng or random.Random()
        self._clock = clock

    async def apply(self, chat_id: int, text: str) -> list[str]:
        st = self._style
        messages = split_messages(text, st.max_messages)
        now = iso(self._clock())

        def op(conn: sqlite3.Connection) -> list[str]:
            row = conn.execute("SELECT recent FROM style_window WHERE chat_id = ?",
                               (chat_id,)).fetchone()
            recent: list[list[int]] = json.loads(row["recent"]) if row else []
            out: list[str] = []
            for msg in messages:
                typos = sum(r[0] for r in recent[-WINDOW:])
                emojis = sum(r[1] for r in recent[-WINDOW:])
                if count_emoji(msg) and emojis >= st.emoji_per_100:
                    msg = strip_emoji(msg) or msg
                has_emoji = 1 if count_emoji(msg) else 0
                brackets = sum(r[2] for r in recent[-WINDOW:] if len(r) > 2)
                if count_smiley(msg) and brackets >= st.bracket_per_100:
                    msg = strip_smiley(msg) or msg
                has_bracket = 1 if count_smiley(msg) else 0
                if st.lowercase:
                    msg = _lower_first(msg)
                if st.drop_final_period:
                    msg = _drop_period(msg)
                typo = 0
                # равномерно: шанс = остаток бюджета / размер окна
                if typos < st.typo_per_100 and self._rng.random() < st.typo_per_100 / WINDOW:
                    changed = make_typo(msg, self._rng)
                    typo = int(changed != msg)
                    msg = changed
                if msg:
                    out.append(msg)
                    recent.append([typo, has_emoji, has_bracket])
            recent = recent[-WINDOW:]
            conn.execute(
                "INSERT INTO style_window (chat_id, recent, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET recent = excluded.recent, "
                "updated_at = excluded.updated_at", (chat_id, json.dumps(recent), now))
            return out

        return await self._db.transaction(op)
