"""Эмбеддинги записей памяти: интерфейс провайдера и фоновая индексация."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Protocol

from alya.core.timeutil import Clock, iso, utcnow
from alya.memory.vectors import Vector, as_vector, encode
from alya.storage.db import Database

log = logging.getLogger(__name__)


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[Vector]: ...


def store_embedding(
    conn: sqlite3.Connection, memory_id: str, model_id: str, vector: Vector,
    expected_content: str, now_iso: str,
) -> bool:
    """Сохранить вектор, только если текст записи не изменился с момента расчёта."""
    vec = as_vector(vector)
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO memory_embeddings (memory_id, model_id, dim, vector, created_at)
        SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM memories WHERE id = ? AND content = ?)
        """,
        (memory_id, model_id, int(vec.shape[0]), encode(vec), now_iso, memory_id,
         expected_content),
    )
    return cur.rowcount > 0


class EmbeddingIndexer:
    """Досчитывает эмбеддинги для записей без вектора текущей модели (кроме working)."""

    def __init__(self, db: Database, embedder: Embedder, *, batch_size: int = 32,
                 clock: Clock = utcnow) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size > 0")
        self._db = db
        self._embedder = embedder
        self._batch = batch_size
        self._clock = clock

    async def pending(self, limit: int) -> list[tuple[str, str]]:
        now_s = iso(self._clock())
        model = self._embedder.model_id

        def op(conn: sqlite3.Connection) -> list[tuple[str, str]]:
            rows = conn.execute(
                """
                SELECT m.id, m.content FROM memories m
                LEFT JOIN memory_embeddings e ON e.memory_id = m.id AND e.model_id = ?
                WHERE e.memory_id IS NULL AND m.layer <> 'working' AND m.superseded_by IS NULL
                  AND (m.expires_at IS NULL OR m.expires_at > ?)
                ORDER BY m.seq LIMIT ?
                """,
                (model, now_s, limit),
            ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

        return await self._db.read(op)

    async def embed_pending(self, limit: int = 256) -> int:
        todo = await self.pending(limit)
        stored = 0
        for start in range(0, len(todo), self._batch):
            chunk = todo[start : start + self._batch]
            try:
                vectors = await self._embedder.embed([text for _, text in chunk])
            except Exception as exc:  # провайдер недоступен — попробуем в следующий цикл
                log.warning("эмбеддинги недоступны: %s", type(exc).__name__)
                break
            if len(vectors) != len(chunk):
                log.warning("провайдер вернул %d векторов вместо %d", len(vectors), len(chunk))
                break
            now_s = iso(self._clock())
            model = self._embedder.model_id

            def op(conn: sqlite3.Connection, chunk: list[tuple[str, str]] = chunk,
                   vectors: list[Vector] = vectors, now_s: str = now_s) -> int:
                return sum(
                    store_embedding(conn, mid, model, vec, text, now_s)
                    for (mid, text), vec in zip(chunk, vectors, strict=True)
                )

            stored += await self._db.transaction(op)
        return stored
