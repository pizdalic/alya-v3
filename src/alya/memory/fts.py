"""Безопасное построение FTS5-запроса: пользовательский текст никогда не попадает как синтаксис.

Каждый токен — только буквы/цифры, берётся в кавычки; токены объединяются через OR.
Для русской морфологии длинные слова ищутся по префиксу (грубый стемминг): «малиновый» → "малинов"*.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

STOPWORDS = frozenset(
    "и в во на не что я ты он она оно мы вы они это как а но да нет с со по к ко у же ли бы то "
    "мне меня тебя тебе его ее её ей им их так там тут вот от до из за для о об при ну уже еще ещё "
    "или если чтобы был была было были есть все всё the a an and or of to in is".split()
)


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def build_match(
    text: str, *, max_tokens: int = 16, prefix_min_len: int = 5, keep_chars: int = 2
) -> str | None:
    """FTS5 MATCH-строка или None, если искать нечего."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN.findall(normalize(text)):
        if len(token) < 2 or token in STOPWORDS:
            continue
        if len(token) >= prefix_min_len:
            stem = token[: max(4, len(token) - keep_chars)]
            term = f'"{stem}"*'
        else:
            term = f'"{token}"'
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= max_tokens:
            break
    return " OR ".join(terms) if terms else None
