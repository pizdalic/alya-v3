"""Группировка похожих записей одной области видимости (union-find по парам выше порога)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from alya.dream.similarity import cosine, jaccard, tokens
from alya.memory.models import Memory
from alya.memory.vectors import Vector


@dataclass(frozen=True, slots=True)
class Item:
    memory: Memory
    vector: Vector | None
    words: frozenset[str]

    @classmethod
    def of(cls, memory: Memory, vector: Vector | None = None) -> Item:
        return cls(memory, vector, tokens(memory.content))


def similarity(a: Item, b: Item) -> float:
    if a.vector is not None and b.vector is not None:
        return cosine(a.vector, b.vector)
    return jaccard(a.words, b.words)


def _comparable(a: Memory, b: Memory) -> bool:
    """Только одна область и один субъект: факты о разных людях не сливаем."""
    return a.scope_key == b.scope_key and a.subject_user_id == b.subject_user_id


def scored_pairs(items: Sequence[Item]) -> list[tuple[float, int, int]]:
    out: list[tuple[float, int, int]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _comparable(items[i].memory, items[j].memory):
                out.append((similarity(items[i], items[j]), i, j))
    out.sort(key=lambda t: (-t[0], t[1], t[2]))
    return out


def build_clusters(items: Sequence[Item], pairs: Sequence[tuple[float, int, int]],
                   threshold: float, max_size: int) -> list[list[Item]]:
    """Кластеры размера ≥ 2. Самые похожие пары соединяются первыми, размер ограничен."""
    parent = list(range(len(items)))
    size = [1] * len(items)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for score, i, j in pairs:
        if score < threshold:
            break
        ri, rj = find(i), find(j)
        if ri == rj or size[ri] + size[rj] > max_size:
            continue
        if size[ri] < size[rj]:
            ri, rj = rj, ri
        parent[rj] = ri
        size[ri] += size[rj]

    groups: dict[int, list[Item]] = {}
    for idx in range(len(items)):
        groups.setdefault(find(idx), []).append(items[idx])
    clusters = [sorted(g, key=lambda it: (it.memory.created_at, it.memory.id))
                for g in groups.values() if len(g) >= 2]
    clusters.sort(key=lambda g: (g[0].memory.created_at, g[0].memory.id))
    return clusters


def related_pairs(items: Sequence[Item], pairs: Sequence[tuple[float, int, int]],
                  low: float, high: float) -> list[tuple[Item, Item]]:
    """Похожие, но не дубли — кандидаты на проверку противоречия."""
    return [(items[i], items[j]) for score, i, j in pairs if low <= score < high]
