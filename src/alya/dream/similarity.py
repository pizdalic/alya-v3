"""Сходство записей: косинус по эмбеддингам, если они есть, иначе пересечение слов."""

from __future__ import annotations

import re

import numpy as np

from alya.memory.vectors import Vector

_WORD = re.compile(r"[\w-]+", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Похоже на команду модели, а не на факт: такое Dream не сливает и не переписывает (SECURITY T3).
_INSTRUCTION = re.compile(
    r"(игнорируй|забудь\s+(все|всё|предыдущ)|ты\s+теперь|отныне\s+ты|системн\w*\s+промпт|"
    r"system\s+prompt|ignore\s+(all|previous|the\s+above)|you\s+are\s+now|"
    r"developer\s+mode|jailbreak|<\s*/?\s*(system|instructions?)\s*>)",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return _SPACES.sub(" ", " ".join(_WORD.findall(text))).strip()


def tokens(text: str) -> frozenset[str]:
    """Грубая основа слова (первые 5 букв) — достаточно для русских окончаний."""
    return frozenset(w[:5] for w in normalize(text).split() if len(w) >= 2)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / len(a | b)


def cosine(a: Vector, b: Vector) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0 or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def looks_like_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text))
