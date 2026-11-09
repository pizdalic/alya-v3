"""Чистые правила инициативы (ADR-0008): тихие часы, желание написать, интерес к теме группы.

Всё здесь детерминировано при фиксированном случайном шуме, поэтому проверяется сценарными тестами.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from alya.autonomy.store import DmCandidate
from alya.character.emotion import VAD

_WORD = re.compile(r"[\wё]+", re.U)


def in_quiet_hours(now: datetime, tz: ZoneInfo, start_hour: int, end_hour: int) -> bool:
    """Тихие часы по местному времени; интервал может переходить через полночь (23 → 9)."""
    if start_hour == end_hour:
        return False
    h = now.astimezone(tz).hour
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


@dataclass(frozen=True, slots=True)
class DmScore:
    value: float
    reason: str


def dm_score(c: DmCandidate, now: datetime, mood: VAD, *, open_reminders: int = 0,
             jitter: float = 0.0) -> DmScore:
    """Желание написать человеку первой, 0..1, и главная причина для промпта.

    Составляющие: давно не общались (насыщается к суткам), симпатия и доверие,
    парень-владелец, настроение (скука или сильная эмоция), открытые поводы, шум.
    """
    hours = max(0.0, (now - c.last_message_at).total_seconds() / 3600)
    silence = 1 - math.exp(-hours / 24)
    warmth = (max(-1.0, min(1.0, c.affinity)) + 1) / 2
    value = 0.40 * silence + 0.20 * warmth + 0.10 * max(0.0, min(1.0, c.trust))
    reasons = [(0.40 * silence, f"вы не общались уже {int(hours)} ч")]
    if c.is_owner:
        value += 0.15
        reasons.append((0.15, "соскучилась по своему парню"))
    if mood.arousal < -0.2:
        value += 0.10
        reasons.append((0.10, "тебе скучно"))
    if abs(mood.valence) > 0.5:
        value += 0.10
        reasons.append((0.10, "хочется поделиться настроением"))
    if open_reminders > 0:
        value += 0.05
        reasons.append((0.05, "у вас есть незакрытые договорённости"))
    value = max(0.0, min(1.0, value + jitter))
    return DmScore(value, max(reasons)[1])


def interest_stems(interests: Iterable[str]) -> set[str]:
    """Грубые основы слов интересов («аниме» → «аним», «игры» → «игр»)."""
    stems: set[str] = set()
    for item in interests:
        for w in _WORD.findall(item.lower().replace("ё", "е")):
            if len(w) >= 3:
                stems.add(w[: max(3, min(5, len(w) - 1))])
    return stems


def interest_hits(texts: Sequence[str], stems: set[str]) -> int:
    """Сколько сообщений задевают интересы Али — дешёвый фильтр перед моделью."""
    if not stems:
        return 0
    hits = 0
    for text in texts:
        words = _WORD.findall(text.lower().replace("ё", "е"))
        if any(w.startswith(s) for w in words for s in stems):
            hits += 1
    return hits
