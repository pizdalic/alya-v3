"""Векторы: float32 в BLOB, косинус на NumPy (обязательный путь),
sqlite-vec — опциональный ускоритель."""

from __future__ import annotations

import importlib
import sqlite3
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from alya.memory.models import MemoryContext
from alya.memory.scope import scoped_where

type Vector = NDArray[np.float32]


def as_vector(values: Any) -> Vector:
    vec = np.asarray(values, dtype=np.float32)
    if vec.ndim != 1 or vec.size == 0:
        raise ValueError("вектор должен быть одномерным и непустым")
    if not np.all(np.isfinite(vec)):
        raise ValueError("вектор содержит NaN/Inf")
    return vec


def encode(vec: Vector) -> bytes:
    return as_vector(vec).astype("<f4", copy=False).tobytes()


def decode(blob: bytes, dim: int) -> Vector:
    if len(blob) != dim * 4:
        raise ValueError("размер BLOB не совпадает с dim")
    return np.frombuffer(blob, dtype="<f4", count=dim).astype(np.float32)


def cosine_top_k(query: Vector, matrix: NDArray[np.float32], k: int) -> list[tuple[int, float]]:
    """Индексы строк matrix и их косинусное сходство с query, по убыванию."""
    if matrix.shape[0] == 0 or k <= 0:
        return []
    q_norm = float(np.linalg.norm(query))
    if q_norm == 0.0:
        return []
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0.0] = np.inf
    sims = (matrix @ query) / (norms * q_norm)
    k = min(k, sims.shape[0])
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx], kind="stable")]
    return [(int(i), float(sims[i])) for i in idx]


class VectorBackend(Protocol):
    name: str

    def top_k(
        self, conn: sqlite3.Connection, ctx: MemoryContext, query: Vector, model_id: str,
        k: int, now_iso: str,
    ) -> list[str]: ...


class NumpyBackend:
    """Полный перебор кандидатов внутри области видимости."""

    name = "numpy"

    def top_k(
        self, conn: sqlite3.Connection, ctx: MemoryContext, query: Vector, model_id: str,
        k: int, now_iso: str,
    ) -> list[str]:
        where, params = scoped_where(ctx, now_iso)
        dim = int(query.shape[0])
        rows = conn.execute(
            f"""
            SELECT e.memory_id, e.vector FROM memory_embeddings e
            JOIN memories m ON m.id = e.memory_id
            WHERE e.model_id = ? AND e.dim = ? AND {where}
            """,
            [model_id, dim, *params],
        ).fetchall()
        if not rows:
            return []
        matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype="<f4").reshape(len(rows), dim)
        return [str(rows[i][0]) for i, _ in cosine_top_k(query, matrix.astype(np.float32), k)]


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Подключить расширение sqlite-vec, если оно установлено. False — недоступно."""
    try:
        module: Any = importlib.import_module("sqlite_vec")
        conn.enable_load_extension(True)
        try:
            module.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
    except (ImportError, AttributeError, sqlite3.Error):
        return False
    return True


class SqliteVecBackend:
    """Тот же отбор по области видимости, расстояние считает sqlite-vec."""

    name = "sqlite_vec"

    def top_k(
        self, conn: sqlite3.Connection, ctx: MemoryContext, query: Vector, model_id: str,
        k: int, now_iso: str,
    ) -> list[str]:
        where, params = scoped_where(ctx, now_iso)
        rows = conn.execute(
            f"""
            SELECT e.memory_id FROM memory_embeddings e
            JOIN memories m ON m.id = e.memory_id
            WHERE e.model_id = ? AND e.dim = ? AND {where}
            ORDER BY vec_distance_cosine(e.vector, ?) ASC, e.memory_id
            LIMIT ?
            """,
            [model_id, int(query.shape[0]), *params, encode(query), k],
        ).fetchall()
        return [str(r[0]) for r in rows]
