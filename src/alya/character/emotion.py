"""Глобальное настроение в модели VAD (valence, arousal, dominance) с затуханием к базе.

Настроение одно на всего персонажа. Каждое событие сдвигает его не больше чем на max_step
по каждой оси (настроение «стабильное, плавное»), а со временем отклонение от базового
уровня характера экспоненциально уменьшается (half-life).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from alya.character.persona import EmotionProfile
from alya.core.timeutil import Clock, iso, parse_iso, utcnow
from alya.storage.db import Database

AXES = ("valence", "arousal", "dominance")


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(lo, min(hi, x))


@dataclass(frozen=True, slots=True)
class VAD:
    valence: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    def clamped(self) -> VAD:
        return VAD(_clamp(self.valence), _clamp(self.arousal), _clamp(self.dominance))

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.valence, self.arousal, self.dominance)


def decay(state: VAD, baseline: VAD, elapsed_s: float, half_life_hours: float) -> VAD:
    """Экспоненциальное возвращение к базе: через half-life остаётся половина отклонения."""
    if elapsed_s <= 0:
        return state
    keep = 0.5 ** (elapsed_s / (half_life_hours * 3600.0))
    return VAD(*(b + (s - b) * keep for s, b in zip(state.as_tuple(), baseline.as_tuple(),
                                                     strict=True))).clamped()


DIARY_MIN = 0.05  # мелкие сдвиги не пишем в дневник


def apply_delta(state: VAD, delta: VAD, profile: EmotionProfile) -> tuple[VAD, VAD]:
    """Применить сдвиг с учётом чувствительности и ограничения шага. Возвращает (новое, факт)."""
    step = profile.max_step
    applied = VAD(*(_clamp(d * profile.sensitivity, -step, step) for d in delta.as_tuple()))
    new = VAD(*(s + a for s, a in zip(state.as_tuple(), applied.as_tuple(), strict=True)))
    new = new.clamped()
    real = VAD(*(n - s for n, s in zip(new.as_tuple(), state.as_tuple(), strict=True)))
    return new, real


def describe(state: VAD) -> str:
    """Короткое словесное описание для промпта (без чисел — модели так понятнее)."""
    v, a, d = state.as_tuple()
    if v > 0.45:
        mood = "очень хорошее, радостное"
    elif v > 0.15:
        mood = "хорошее"
    elif v > -0.15:
        mood = "ровное"
    elif v > -0.45:
        mood = "немного грустное"
    else:
        mood = "плохое, обиженное"
    energy = "бодрая" if a > 0.3 else "сонная, вялая" if a < -0.3 else "спокойная"
    conf = "уверенная" if d > 0.3 else "робкая" if d < -0.3 else ""
    return ", ".join(x for x in (f"настроение {mood}", energy, conf) if x)


class EmotionStore:
    """Хранит настроение в character_state и пишет эмоциональный дневник."""

    def __init__(self, db: Database, persona_id: str, profile: EmotionProfile,
                 *, clock: Clock = utcnow) -> None:
        self._db = db
        self._persona = persona_id
        self._profile = profile
        self._clock = clock
        b = profile.baseline
        self._baseline = VAD(b.valence, b.arousal, b.dominance)

    @property
    def baseline(self) -> VAD:
        return self._baseline

    def _load(self, conn: sqlite3.Connection, now: datetime) -> VAD:
        row = conn.execute(
            "SELECT persona, valence, arousal, dominance, updated_at FROM character_state "
            "WHERE id = 1").fetchone()
        if row is None or row["persona"] != self._persona:
            # новый старт или переключили характер — начинаем с его базы
            return self._baseline
        state = VAD(row["valence"], row["arousal"], row["dominance"])
        elapsed = (now - parse_iso(row["updated_at"])).total_seconds()
        return decay(state, self._baseline, elapsed, self._profile.half_life_hours)

    def _save(self, conn: sqlite3.Connection, state: VAD, now: datetime) -> None:
        conn.execute(
            "INSERT INTO character_state (id, persona, valence, arousal, dominance, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET persona = excluded.persona, "
            "valence = excluded.valence, arousal = excluded.arousal, "
            "dominance = excluded.dominance, updated_at = excluded.updated_at",
            (self._persona, *state.as_tuple(), iso(now)))

    async def current(self) -> VAD:
        now = self._clock()
        return await self._db.read(lambda conn: self._load(conn, now))

    async def feel(self, delta: VAD, reason: str, *, chat_id: int | None = None,
                   user_id: int | None = None) -> VAD:
        """Атомарно: затухание → сдвиг → сохранение → запись в дневник (если сдвиг заметен)."""
        now = self._clock()
        reason = " ".join(reason.split())[:300] or "без причины"

        def op(conn: sqlite3.Connection) -> VAD:
            state = self._load(conn, now)
            new, real = apply_delta(state, delta, self._profile)
            self._save(conn, new, now)
            if max(abs(x) for x in real.as_tuple()) >= DIARY_MIN:
                conn.execute(
                    "INSERT INTO emotion_diary (chat_id, user_id, reason, d_valence, d_arousal, "
                    "d_dominance, valence, arousal, dominance, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, user_id, reason, *real.as_tuple(), *new.as_tuple(), iso(now)))
            return new

        return await self._db.transaction(op)

    async def reasons(self, chat_id: int, *, hours: float = 6.0,
                      limit: int = 3) -> list[str]:
        """Недавние причины настроения только из этого чата (чужие чаты не протекают)."""
        since = iso(self._clock() - timedelta(hours=hours))
        rows = await self._db.fetchall(
            "SELECT reason FROM emotion_diary WHERE chat_id = ? AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?", (chat_id, since, limit))
        return [str(r["reason"]) for r in reversed(rows)]

    async def diary(self, limit: int = 20) -> list[sqlite3.Row]:
        return await self._db.fetchall(
            "SELECT * FROM emotion_diary ORDER BY id DESC LIMIT ?", (limit,))
