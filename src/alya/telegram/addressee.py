"""Addressee Detector: кому адресовано сообщение в группе.

Три ступени (ADR-0007):
1. Явные сигналы: реплай Але, упоминание, обращение по имени — ей.
   Реплай на чужое сообщение или упоминание другого человека — не ей.
2. Нить разговора: кому Аля писала последней, как давно, был ли вопрос,
   сколько чужих реплик вклинилось. Отсюда — оценка от 0 до 1.
3. Серая зона (между LOW и HIGH) — небольшой LLM-классификатор.

Нити живут в памяти процесса: это минуты, после перезапуска нить
откроется заново первой же репликой Али.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from alya.core.timeutil import Clock, utcnow
from alya.llm.chat import ChatModel
from alya.telegram.models import IncomingMessage

log = logging.getLogger(__name__)

HIGH = 0.65
LOW = 0.35
_BURST_S = 60.0  # части одного ответа Али
_YOU = re.compile(r"(?<!\w)(ты|тебе|тебя|тобой|твой|твоя|твое|твои|сама)(?!\w)")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


@dataclass(frozen=True, slots=True)
class Decision:
    addressed: bool
    reason: str
    score: float | None = None


@dataclass(slots=True)
class _Thread:
    addressees: frozenset[int]  # пусто — Аля писала всем
    last_alya_at: datetime
    asked_question: bool
    chatter: int = 0


class AddresseeClassifier(Protocol):
    async def probability(self, context: Sequence[str], text: str) -> float: ...


class LLMAddresseeClassifier:
    """Спрашивает модель utility, обращено ли сообщение к Але. Возвращает вероятность."""

    def __init__(self, chat: ChatModel, *, name: str = "Аля") -> None:
        self._chat = chat
        self._name = name

    async def probability(self, context: Sequence[str], text: str) -> float:
        system = (
            f"Ты анализируешь групповой чат. Участница по имени {self._name} — одна из "
            "собеседниц. Определи, обращено ли ПОСЛЕДНЕЕ сообщение к ней (ответ на её "
            "реплику или вопрос ей), а не к другим людям. Ответь строго JSON "
            '{"p": число от 0 до 1}.'
        )
        transcript = "\n".join(context[-8:])
        user = f"Переписка:\n{transcript}\n\nПоследнее сообщение:\n{text}"
        raw = await self._chat.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0, max_tokens=20, json_mode=True)
        match = re.search(r"\{.*\}", raw, re.S)
        if match is None:
            raise ValueError("классификатор вернул не JSON")
        p = float(json.loads(match.group(0))["p"])
        return min(1.0, max(0.0, p))


@dataclass(slots=True)
class _ChatState:
    thread: _Thread | None = None
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=12))


class AddresseeDetector:
    def __init__(self, me_id: int, me_username: str | None, names: Iterable[str], *,
                 thread_ttl_s: float = 300.0, classifier: AddresseeClassifier | None = None,
                 clock: Clock = utcnow) -> None:
        self._me = me_id
        self._username = (me_username or "").lower().lstrip("@") or None
        words = sorted({_norm(n).strip() for n in names if n.strip()}, key=len, reverse=True)
        self._name_re = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)")
                         if words else None)
        self._ttl = thread_ttl_s
        self._classifier = classifier
        self._clock = clock
        self._chats: dict[int, _ChatState] = {}

    # --- состояние ----------------------------------------------------------

    def _state(self, chat_id: int) -> _ChatState:
        return self._chats.setdefault(chat_id, _ChatState())

    def _alive(self, chat_id: int) -> _Thread | None:
        state = self._chats.get(chat_id)
        if state is None or state.thread is None:
            return None
        age = (self._clock() - state.thread.last_alya_at).total_seconds()
        if age > self._ttl:
            state.thread = None
            return None
        return state.thread

    def note_alya_message(self, chat_id: int, text: str, addressees: Iterable[int]) -> None:
        """Аля (или владелец с её аккаунта) написала в чат — нить открывается заново."""
        now = self._clock()
        state = self._state(chat_id)
        prev = self._alive(chat_id)
        burst = prev is not None and (now - prev.last_alya_at).total_seconds() <= _BURST_S
        targets = frozenset(a for a in addressees if a != self._me)
        if not targets and burst and prev is not None:
            targets = prev.addressees
        asked = "?" in text or (burst and prev is not None and prev.asked_question)
        state.thread = _Thread(targets, now, asked)
        state.recent.append(f"Аля: {text}")

    def forget_chat(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)

    # --- решение ------------------------------------------------------------

    def _mentions_me(self, msg: IncomingMessage) -> bool:
        if self._me in msg.mentioned_user_ids:
            return True
        return self._username is not None and self._username in msg.mentioned_usernames

    def _says_name(self, text: str) -> bool:
        return self._name_re is not None and self._name_re.search(_norm(text)) is not None

    def thread_score(self, chat_id: int, msg: IncomingMessage) -> float | None:
        thread = self._alive(chat_id)
        if thread is None:
            return None
        elapsed = (self._clock() - thread.last_alya_at).total_seconds()
        score = 0.45
        if elapsed <= 60:
            score += 0.2
        elif elapsed <= 180:
            score += 0.1
        if thread.asked_question:
            score += 0.15
        score -= 0.15 * min(thread.chatter, 3)
        if _YOU.search(_norm(msg.text)):
            score += 0.1
        score += 0.05 if len(msg.text) <= 80 else -0.05
        return round(min(1.0, max(0.0, score)), 3)

    async def _evaluate(self, msg: IncomingMessage) -> Decision:
        if msg.is_private:
            return Decision(True, "private")
        if msg.reply_to_user_id == self._me:
            return Decision(True, "reply")
        if self._mentions_me(msg):
            return Decision(True, "mention")
        if self._says_name(msg.text):
            return Decision(True, "name")
        if msg.reply_to_user_id is not None:
            return Decision(False, "reply_other")
        if msg.mentioned_user_ids or msg.mentioned_usernames:
            return Decision(False, "mention_other")
        thread = self._alive(msg.chat_id)
        if thread is None:
            return Decision(False, "no_thread")
        if thread.addressees and msg.sender_id not in thread.addressees:
            return Decision(False, "other_person")
        score = self.thread_score(msg.chat_id, msg)
        assert score is not None
        if score >= HIGH:
            return Decision(True, "thread", score)
        if score < LOW or self._classifier is None:
            return Decision(False, "low_score" if score < LOW else "unclear", score)
        try:
            p = await self._classifier.probability(list(self._state(msg.chat_id).recent),
                                                   f"{msg.display_name}: {msg.text}")
        except Exception as exc:  # сеть, лимиты, мусор от модели — не отвечаем
            log.warning("классификатор адресата недоступен: %s", type(exc).__name__)
            return Decision(False, "unclear", score)
        return Decision(p >= 0.5, "classifier", p)

    async def decide(self, msg: IncomingMessage) -> Decision:
        decision = await self._evaluate(msg)
        if not msg.is_private:
            state = self._state(msg.chat_id)
            state.recent.append(f"{msg.display_name}: {msg.text}")
            thread = self._alive(msg.chat_id)
            if thread is not None and not decision.addressed:
                thread.chatter += 1
        return decision
