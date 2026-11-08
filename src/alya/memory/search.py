"""Гибридный поиск: FTS5 + векторы, слияние Reciprocal Rank Fusion, модификаторы.

score = Σ 1/(k + rank) × (0.5 + 0.5·confidence) × свежесть × (1 + 0.05·min(usage, 10)).
Свежесть = 0.5 + 0.5·2^(−возраст/half_life); для постоянных записей = 1.
Без эмбеддингов (или при ошибке провайдера) работает только FTS5, это отражается в результате.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from alya.core.timeutil import Clock, iso, parse_iso, utcnow
from alya.memory.fts import build_match
from alya.memory.indexer import Embedder
from alya.memory.models import Memory, MemoryContext
from alya.memory.repository import MemoryRepository
from alya.memory.scope import scoped_where
from alya.memory.vectors import NumpyBackend, VectorBackend, as_vector

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchHit:
    memory: Memory
    score: float
    fts_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[SearchHit]
    used_fts: bool
    used_vectors: bool
    degraded: str | None = None


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    if k <= 0:
        raise ValueError("k > 0")
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] += 1.0 / (k + rank)
    return dict(scores)


def fts_candidates(
    conn: sqlite3.Connection, ctx: MemoryContext, match: str, limit: int, now_iso: str
) -> list[str]:
    where, params = scoped_where(ctx, now_iso)
    rows = conn.execute(
        f"""
        SELECT m.id FROM memories_fts
        JOIN memories m ON m.seq = memories_fts.rowid
        WHERE memories_fts MATCH ? AND {where}
        ORDER BY bm25(memories_fts), m.seq DESC
        LIMIT ?
        """,
        [match, *params, limit],
    ).fetchall()
    return [str(r[0]) for r in rows]


def modifier(memory: Memory, now: datetime, half_life_days: float) -> float:
    conf = 0.5 + 0.5 * memory.confidence
    if memory.is_permanent:
        fresh = 1.0
    else:
        age_days = max(0.0, (now - parse_iso(memory.updated_at)).total_seconds() / 86400)
        fresh = 0.5 + 0.5 * 2.0 ** (-age_days / half_life_days)
    usage = 1.0 + 0.05 * min(memory.usage_count, 10)
    return conf * fresh * usage


class MemorySearch:
    def __init__(
        self,
        repo: MemoryRepository,
        embedder: Embedder | None = None,
        backend: VectorBackend | None = None,
        *,
        rrf_k: int = 60,
        candidates: int = 50,
        half_life_days: float = 90.0,
        clock: Clock = utcnow,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._backend = backend or NumpyBackend()
        self._rrf_k = rrf_k
        self._candidates = candidates
        self._half_life = half_life_days
        self._clock = clock

    async def search(
        self, ctx: MemoryContext, query: str, *, k: int = 8, touch: bool = True
    ) -> SearchResult:
        now = self._clock()
        now_s = iso(now)
        db = self._repo.db
        match = build_match(query)
        fts_ids: list[str] = []
        if match is not None:
            fts_ids = await db.read(
                lambda c: fts_candidates(c, ctx, match, self._candidates, now_s))

        vec_ids: list[str] = []
        used_vectors = False
        degraded: str | None = None if self._embedder else "эмбеддинги не настроены"
        if self._embedder is not None and query.strip():
            try:
                qv = as_vector((await self._embedder.embed([query]))[0])
            except Exception as exc:
                degraded = f"эмбеддинги недоступны: {type(exc).__name__}"
                log.warning("поиск без векторов: %s", type(exc).__name__)
            else:
                model = self._embedder.model_id
                vec_ids = await db.read(
                    lambda c: self._backend.top_k(c, ctx, qv, model, self._candidates, now_s))
                used_vectors = True

        fused = rrf([fts_ids, vec_ids], self._rrf_k)
        if not fused:
            return SearchResult([], match is not None, used_vectors, degraded)
        memories = await self._repo.get_many(ctx, list(fused))  # повторная проверка видимости
        fts_rank = {mid: i for i, mid in enumerate(fts_ids, start=1)}
        vec_rank = {mid: i for i, mid in enumerate(vec_ids, start=1)}
        hits = [
            SearchHit(m, fused[m.id] * modifier(m, now, self._half_life),
                      fts_rank.get(m.id), vec_rank.get(m.id))
            for m in memories
        ]
        hits.sort(key=lambda h: (-h.score, h.memory.id))
        hits = hits[:k]
        if touch and hits:
            await self._repo.touch(h.memory.id for h in hits)
        return SearchResult(hits, match is not None, used_vectors, degraded)
