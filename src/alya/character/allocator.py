"""Context Allocator: собирает промпт по слоям в пределах бюджета токенов.

Слой 0 (ядро персонажа + правила) включается всегда. Остальное — по приоритету:
состояние и отношения → память (по убыванию релевантности) → история (от новых к старым).
Порядок в итоговом тексте фиксированный, а урезается всегда самое неважное.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 3.2  # грубая оценка для русского текста; с запасом


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


@dataclass(frozen=True, slots=True)
class HistoryLine:
    author: str  # имя отправителя или «ты» для реплик Али
    text: str
    is_alya: bool = False


@dataclass(slots=True)
class Allocation:
    core: str
    state: str
    memories: list[str] = field(default_factory=list)
    history: list[HistoryLine] = field(default_factory=list)
    dropped_memories: int = 0
    dropped_history: int = 0
    tokens: int = 0


class ContextAllocator:
    def __init__(self, budget_tokens: int = 6000, *, line_cap: int = 600) -> None:
        if budget_tokens < 500:
            raise ValueError("бюджет контекста слишком мал")
        self.budget = budget_tokens
        self.line_cap = line_cap

    def _cap(self, text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= self.line_cap else text[: self.line_cap - 1] + "…"

    def allocate(self, *, core: str, state: str, memories: list[str],
                 history: list[HistoryLine], reserve: int = 800) -> Allocation:
        """memories — уже по убыванию релевантности, history — в хронологическом порядке."""
        used = estimate_tokens(core) + estimate_tokens(state) + reserve
        alloc = Allocation(core=core, state=state)
        # история важнее для связности: сначала гарантируем последние реплики (до 40% бюджета)
        hist_budget = int(self.budget * 0.4)
        picked: list[HistoryLine] = []
        hist_used = 0
        for line in reversed(history):
            line = HistoryLine(line.author, self._cap(line.text), line.is_alya)
            cost = estimate_tokens(f"{line.author}: {line.text}") + 1
            if used + hist_used + cost > self.budget or hist_used + cost > hist_budget:
                break
            picked.append(line)
            hist_used += cost
        used += hist_used
        for mem in memories:
            mem = self._cap(mem)
            cost = estimate_tokens(mem) + 1
            if used + cost > self.budget:
                break
            alloc.memories.append(mem)
            used += cost
        alloc.dropped_memories = len(memories) - len(alloc.memories)
        # остаток бюджета — ещё более старой истории
        for line in reversed(history[: len(history) - len(picked)]):
            line = HistoryLine(line.author, self._cap(line.text), line.is_alya)
            cost = estimate_tokens(f"{line.author}: {line.text}") + 1
            if used + cost > self.budget:
                break
            picked.append(line)
            used += cost
        alloc.history = list(reversed(picked))
        alloc.dropped_history = len(history) - len(picked)
        alloc.tokens = used - reserve
        return alloc
