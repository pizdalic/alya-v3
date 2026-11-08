"""Бенчмарк векторного поиска NumPy (и sqlite-vec, если установлен).

Запуск: PYTHONPATH=src python3 scripts/bench_vectors.py [--dim 1536] [--sizes 1000 5000 20000]
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import time
from datetime import UTC, datetime

import numpy as np

from alya.core.ids import new_ulid
from alya.core.timeutil import iso
from alya.memory.models import MemoryContext
from alya.memory.vectors import NumpyBackend, SqliteVecBackend, encode, load_sqlite_vec
from alya.storage.entities import upsert_chat, upsert_user
from alya.storage.migrator import apply_migrations

UID = 1


def build(n: int, dim: int, rng: np.random.Generator) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn)
    now = iso(datetime.now(UTC))
    upsert_user(conn, UID, now)
    upsert_chat(conn, UID, "private", now)
    conn.execute("BEGIN")
    for i in range(n):
        mid = new_ulid()
        conn.execute(
            "INSERT INTO memories (id, scope, owner_user_id, chat_id, subject_user_id, layer, "
            "category, content, provenance, confidence, created_at, updated_at) "
            "VALUES (?, 'private', ?, ?, ?, 'long', 'fact', ?, '{}', 0.7, ?, ?)",
            (mid, UID, UID, UID, f"запись {i}", now, now))
        vec = rng.standard_normal(dim).astype(np.float32)
        conn.execute("INSERT INTO memory_embeddings VALUES (?, 'bench', ?, ?, ?)",
                     (mid, dim, encode(vec), now))
    conn.execute("COMMIT")
    return conn


def bench(conn: sqlite3.Connection, backend, dim: int, rng: np.random.Generator,  # type: ignore[no-untyped-def]
          runs: int) -> float:
    ctx = MemoryContext.private(UID)
    now = iso(datetime.now(UTC))
    times = []
    for _ in range(runs):
        q = rng.standard_normal(dim).astype(np.float32)
        t0 = time.perf_counter()
        backend.top_k(conn, ctx, q, "bench", 50, now)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dim", type=int, default=1536)
    p.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000, 20000])
    p.add_argument("--runs", type=int, default=7)
    args = p.parse_args()
    rng = np.random.default_rng(0)
    print(f"dim={args.dim}, top-50, медиана из {args.runs} запросов")
    for n in args.sizes:
        conn = build(n, args.dim, rng)
        line = f"N={n:>6}: numpy {bench(conn, NumpyBackend(), args.dim, rng, args.runs):8.1f} мс"
        if load_sqlite_vec(conn):
            line += f" | sqlite-vec {bench(conn, SqliteVecBackend(), args.dim, rng, args.runs):8.1f} мс"
        print(line, flush=True)
        conn.close()


if __name__ == "__main__":
    main()
