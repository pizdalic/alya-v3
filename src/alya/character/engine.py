"""Character Engine: настроение + отношения + память → план (этап 1) → ответ (этап 2) → стиль.

Вызывающая сторона (Telegram-слой, CLI) отвечает за то, чтобы пользователь и чат уже были
в таблицах users/chats: от них каскадно зависят отношения и дневник (и их удаление в Forget).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from alya.character.allocator import Allocation, ContextAllocator, HistoryLine
from alya.character.emotion import VAD, EmotionStore, describe
from alya.character.persona import Character
from alya.character.planner import PLAN_SCHEMA, Plan, parse_plan
from alya.character.relationships import Relationship, RelationshipStore
from alya.character.style import Stylizer
from alya.core.timeutil import Clock, utcnow
from alya.llm.chat import ChatMessage, ChatModel
from alya.memory.models import MemoryContext
from alya.memory.service import MemoryService

log = logging.getLogger(__name__)

_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
# Явное заявление «я человек / не бот» — недопустимо в ответ на серьёзный вопрос.
_DENIAL = re.compile(
    r"(я\s+(не\s+)?(бот|ии|нейросеть|программа)\b(?<!я бот)(?<!я ии))|"
    r"(я\s+(живой|живая|настоящий|настоящая)\s+(человек|девушка))|"
    r"(я\s+(обычный|обычная)?\s*человек)|(не\s+бот\s+я)",
    re.I)
_SPEAKER_PREFIX = re.compile(r"^\s*(аля|alya)\s*:\s*", re.I)


def _denies_being_ai(text: str) -> bool:
    low = text.lower().replace("ё", "е")
    if re.search(r"\bя\s+(ии|бот|нейросеть|ии-персонаж)\b", low) and not re.search(
            r"\bя\s+не\s+(ии|бот|нейросеть)\b", low):
        return False  # признала
    return bool(_DENIAL.search(low))


@dataclass(frozen=True, slots=True)
class Turn:
    ctx: MemoryContext
    text: str
    sender_name: str
    history: list[HistoryLine] = field(default_factory=list)
    addressed: bool = True  # сообщение адресовано Але (личка, ответ, упоминание или по смыслу)


@dataclass(frozen=True, slots=True)
class Reply:
    messages: list[str]
    plan: Plan
    plan_degraded: bool
    mood: VAD
    relationship: Relationship
    context_tokens: int


class CharacterEngine:
    def __init__(
        self,
        character: Character,
        chat: ChatModel,
        *,
        emotions: EmotionStore,
        relationships: RelationshipStore,
        stylizer: Stylizer,
        allocator: ContextAllocator,
        memory: MemoryService | None = None,
        planner: ChatModel | None = None,
        timezone: str = "Asia/Yekaterinburg",
        recall_k: int = 8,
        temperature: float = 0.9,
        max_reply_tokens: int = 400,
        clock: Clock = utcnow,
    ) -> None:
        self.character = character
        self._chat = chat
        self._planner = planner or chat
        self._emotions = emotions
        self._relationships = relationships
        self._stylizer = stylizer
        self._allocator = allocator
        self._memory = memory
        self._tz = ZoneInfo(timezone)
        self._k = recall_k
        self._temperature = temperature
        self._max_tokens = max_reply_tokens
        self._clock = clock

    # --- контекст -------------------------------------------------------------

    def _state_text(self, turn: Turn, mood: VAD, rel: Relationship) -> str:
        local = self._clock().astimezone(self._tz)
        where = "личная переписка" if turn.ctx.chat_kind == "private" else "групповой чат"
        return "\n".join([
            f"Сейчас {_WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y %H:%M} (Екатеринбург).",
            f"Твоё состояние: {describe(mood)}.",
            f"Где: {where}. Собеседник: {turn.sender_name} — {rel.describe()}.",
        ])

    async def _recall(self, turn: Turn) -> list[str]:
        if self._memory is None or self._k <= 0 or not turn.text.strip():
            return []
        try:
            result = await self._memory.recall(turn.ctx, turn.text, k=self._k)
        except Exception:
            log.warning("память недоступна, отвечаю без неё", exc_info=True)
            return []
        return [hit.memory.content for hit in result.hits]

    @staticmethod
    def _system(alloc: Allocation, extra: str = "") -> str:
        parts = [alloc.core, "", alloc.state]
        if alloc.memories:
            parts += ["", "Что ты помнишь (может быть неточно):",
                      *[f"[{i}] {m}" for i, m in enumerate(alloc.memories)]]
        if extra:
            parts += ["", extra]
        return "\n".join(parts)

    @staticmethod
    def _transcript(alloc: Allocation, turn: Turn) -> str:
        lines = [f"{'ты' if h.is_alya else h.author}: {h.text}" for h in alloc.history]
        head = "Недавняя переписка:\n" + "\n".join(lines) if lines else "Переписки ещё нет."
        return f"{head}\n\nНовое сообщение от {turn.sender_name}: {turn.text}"

    # --- генерация ------------------------------------------------------------

    async def _plan(self, alloc: Allocation, turn: Turn) -> tuple[Plan, bool]:
        addressed = "Сообщение адресовано тебе." if turn.addressed else (
            "Сообщение, похоже, не тебе — отвечай, только если это уместно.")
        messages: list[ChatMessage] = [
            {"role": "system", "content": self._system(alloc)},
            {"role": "user", "content": (
                f"{self._transcript(alloc, turn)}\n\n{addressed}\n"
                "Не пиши ответ. Составь план ответа и верни ТОЛЬКО JSON по схеме:\n"
                f"{PLAN_SCHEMA}")},
        ]
        try:
            raw = await self._planner.complete(messages, temperature=0.3, max_tokens=500,
                                               json_mode=True)
        except Exception:
            log.warning("планировщик недоступен, используется план по умолчанию", exc_info=True)
            return Plan(), True
        return parse_plan(raw)

    async def _answer(self, alloc: Allocation, turn: Turn, plan: Plan) -> str:
        guide = [f"План ответа: {plan.intent or 'ответить естественно'}."]
        if plan.tone:
            guide.append(f"Тон: {plan.tone}.")
        guide += [f"Избегай: {c}." for c in plan.constraints]
        if plan.sincere_ai_question:
            guide.append("Тебя всерьёз спросили, ИИ ли ты: честно признай, что ты ИИ-персонаж, "
                         "в своём стиле, не выходя из образа.")
        messages: list[ChatMessage] = [
            {"role": "system", "content": self._system(alloc, " ".join(guide))},
            {"role": "user", "content": (
                f"{self._transcript(alloc, turn)}\n\n"
                "Напиши ответ Али. Только текст сообщений, каждое с новой строки, "
                "без кавычек и без подписи.")},
        ]
        text = await self._chat.complete(messages, temperature=self._temperature,
                                         max_tokens=self._max_tokens)
        text = _SPEAKER_PREFIX.sub("", text.strip()).strip().strip("«»\"")
        return text

    async def respond(self, turn: Turn) -> Reply:
        ctx = turn.ctx
        mood = await self._emotions.current()
        rel = await self._relationships.get(ctx.user_id, ctx.chat_id)
        memories = await self._recall(turn)
        alloc = self._allocator.allocate(core=self.character.core_prompt(),
                                         state=self._state_text(turn, mood, rel),
                                         memories=memories, history=turn.history)
        plan, degraded = await self._plan(alloc, turn)

        e = plan.emotion
        mood = await self._emotions.feel(
            VAD(e.valence, e.arousal, e.dominance),
            plan.emotion_reason or f"сообщение от {turn.sender_name}",
            chat_id=ctx.chat_id, user_id=ctx.user_id)
        rel = await self._relationships.interact(ctx.user_id, ctx.chat_id,
                                                 d_affinity=plan.affinity * 0.1,
                                                 d_trust=plan.trust * 0.1)

        should_reply = self._decide(plan, turn, mood)
        if should_reply:
            alloc.state = self._state_text(turn, mood, rel)
            text = await self._answer(alloc, turn, plan)
            if plan.sincere_ai_question and _denies_being_ai(text):
                log.info("ответ отрицал ИИ на серьёзный вопрос — заменён честным")
                text = self.character.core.honest_reply
            messages = await self._stylizer.apply(ctx.chat_id, text)
        else:
            messages = []
        await self._remember(turn, plan)
        return Reply(messages, plan, degraded, mood, rel, alloc.tokens)

    def _decide(self, plan: Plan, turn: Turn, mood: VAD) -> bool:
        if plan.sincere_ai_question:
            return True  # на серьёзный вопрос об ИИ не молчим
        if plan.should_reply:
            return True
        if not turn.addressed:
            return False
        # промолчать на обращённое к ней сообщение можно только всерьёз обидевшись
        return mood.valence > self.character.persona.emotion.ignore_below

    async def _remember(self, turn: Turn, plan: Plan) -> None:
        if self._memory is None or not plan.remember:
            return
        ctx = turn.ctx
        provenance = {"source": "dialog", "stage": "planner"}
        for fact in plan.remember:
            try:
                if ctx.chat_kind == "private":
                    await self._memory.remember(ctx, fact, scope="private", layer="short",
                                                provenance=provenance, confidence=0.6)
                else:
                    await self._memory.remember(ctx, fact, scope="chat", layer="short",
                                                subject_user_id=ctx.user_id,
                                                provenance=provenance, confidence=0.6)
            except Exception:
                log.warning("не удалось сохранить факт в память", exc_info=True)
