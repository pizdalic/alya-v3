"""Этап 1 двухэтапной генерации: план ответа в JSON.

Модель решает, отвечать ли, с каким намерением и тоном, как изменилось настроение и отношение,
что стоит запомнить. Ответ разбирается терпимо: числа обрезаются по диапазонам, мусор
отбрасывается, а при полном провале используется безопасный план по умолчанию.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLAN_SCHEMA = """{
  "should_reply": true,            // false — промолчать (только если обидели или сообщение не тебе)
  "intent": "что хочешь сделать ответом, коротко",
  "tone": "тон ответа, 1-3 слова",
  "memories_used": [0, 2],         // номера пунктов памяти, на которые опираешься
  "emotion": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0},  // как ИМЕННО это сообщение меняет твоё настроение, -1..1; не изменилось — все 0. Грубость и ссоры между другими людьми и старые сообщения из переписки тебя не задевают
  "emotion_reason": "что случилось и кто, коротко от первого лица; \"\" если настроение не изменилось",
  "affinity": 0.0,                 // как меняется симпатия к собеседнику, -1..1
  "trust": 0.0,                    // как меняется доверие, -1..1
  "sincere_ai_question": false,    // true — тебя всерьёз и прямо спрашивают, ИИ ли ты
  "remember": [],                  // до 2 новых важных фактов о собеседнике, от третьего лица
  "constraints": [],               // чего избегать в ответе
  "reaction": "",                  // эмодзи-реакция на сообщение собеседника или "" (редко, только к месту)
  "sticker": "",                   // эмодзи стикера после ответа или "" (редко)
  "voice": false,                  // true — ответить голосовым (очень редко: просят или очень к месту)
  "selfie": "",                    // просят твоё фото — что на нём, коротко; иначе ""
  "search": "",                    // нужен свежий факт из интернета — поисковый запрос; иначе ""
  "remind_at": "",                 // просят напомнить — когда, "ГГГГ-ММ-ДД ЧЧ:ММ" по Екатеринбургу; иначе ""
  "remind_text": ""                // о чём напомнить, коротко
}"""

# Реакции, которые Telegram разрешает по умолчанию; грубые убраны — Аля милая.
REACTIONS = frozenset({
    "👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "😢", "🎉", "🤩", "🙏", "👌",
    "🕊", "🥱", "😍", "🐳", "❤‍🔥", "🌚", "💯", "🤣", "⚡", "🏆", "💔", "🤨", "😐", "🍓",
    "💋", "😈", "😴", "😭", "🤓", "👻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗",
    "🫡", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "🙊", "😎", "👾",
    "🤷", "😡",
})


def _emoji(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    e = value.replace("\ufe0f", "").strip()
    return e if 0 < len(e) <= 4 and not any(ch.isalnum() for ch in e) else ""


def _clamp(value: Any, lo: float, hi: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, x)) if math.isfinite(x) else 0.0


def _short(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if value is not None else ""


class EmotionDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    valence: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    @field_validator("valence", "arousal", "dominance", mode="before")
    @classmethod
    def _range(cls, v: Any) -> float:
        return _clamp(v, -1, 1)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    should_reply: bool = True
    intent: str = ""
    tone: str = ""
    memories_used: list[int] = Field(default_factory=list)
    emotion: EmotionDelta = Field(default_factory=EmotionDelta)
    emotion_reason: str = ""
    affinity: float = 0.0
    trust: float = 0.0
    sincere_ai_question: bool = False
    remember: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    reaction: str = ""
    sticker: str = ""
    voice: bool = False
    selfie: str = ""
    search: str = ""
    remind_at: str = ""
    remind_text: str = ""

    @field_validator("remind_at", mode="before")
    @classmethod
    def _remind_at(cls, v: Any) -> str:
        return _short(v, 40) if isinstance(v, str) else ""

    @field_validator("remind_text", mode="before")
    @classmethod
    def _remind_text(cls, v: Any) -> str:
        return _short(v, 300) if isinstance(v, str) else ""

    @field_validator("reaction", mode="before")
    @classmethod
    def _reaction(cls, v: Any) -> str:
        e = _emoji(v)
        return e if e in REACTIONS else ""

    @field_validator("sticker", mode="before")
    @classmethod
    def _sticker(cls, v: Any) -> str:
        return _emoji(v)

    @field_validator("selfie", "search", mode="before")
    @classmethod
    def _request200(cls, v: Any) -> str:
        return _short(v, 200) if isinstance(v, str) else ""

    @field_validator("intent", "emotion_reason", mode="before")
    @classmethod
    def _text200(cls, v: Any) -> str:
        return _short(v, 200)

    @field_validator("tone", mode="before")
    @classmethod
    def _text60(cls, v: Any) -> str:
        return _short(v, 60)

    @field_validator("affinity", "trust", mode="before")
    @classmethod
    def _range(cls, v: Any) -> float:
        return _clamp(v, -1, 1)

    @field_validator("memories_used", mode="before")
    @classmethod
    def _ints(cls, v: Any) -> list[int]:
        if not isinstance(v, list):
            return []
        out = []
        for x in v[:20]:
            if isinstance(x, bool):
                continue
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    @field_validator("remember", mode="before")
    @classmethod
    def _facts(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        facts = [_short(x, 300) for x in v if isinstance(x, str)]
        return [f for f in facts if len(f) >= 3][:2]

    @field_validator("constraints", mode="before")
    @classmethod
    def _constraints(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [_short(x, 150) for x in v if isinstance(x, str) and x.strip()][:5]

    @field_validator("should_reply", "sincere_ai_question", "voice", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"true", "1", "yes", "да"}
        return bool(v)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_plan(text: str) -> tuple[Plan, bool]:
    """Возвращает (план, degraded). degraded=True — план по умолчанию из-за ошибки разбора."""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return Plan(), True
    try:
        data = json.loads(candidate[start : end + 1])
        if not isinstance(data, dict):
            return Plan(), True
        return Plan.model_validate(data), False
    except (ValueError, ValidationError):
        return Plan(), True
