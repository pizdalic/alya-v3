"""Dream Engine: один прогон консолидации памяти.

Гарантии:
- Работает строго внутри одной области (ScopeKey) и одного субъекта: личное не смешивается.
- Каждый кластер / пара применяется в своей транзакции. Перед записью источники
  перечитываются: если их успели изменить или удалить — кластер пропускается.
  Падение на середине оставляет уже закоммиченные кластеры и нетронутые остальные,
  полуслияний не бывает (источники помечаются заменёнными в той же транзакции,
  где создаётся новая запись).
- Защищённые записи (is_permanent или confidence ≥ protected_confidence — подтверждены
  пользователем) никогда не переписываются, не теряют уверенность и не заменяются.
- LLM видит записи как данные; ответ, похожий на инструкцию, отбрасывается.
- У каждой созданной записи provenance: номер прогона и id источников.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from alya.core.ids import new_ulid
from alya.core.timeutil import Clock, iso, utcnow
from alya.dream.cluster import Item, build_clusters, related_pairs, scored_pairs, similarity
from alya.dream.judge import DreamJudge, Relation
from alya.dream.similarity import looks_like_instruction, normalize
from alya.memory.models import Memory, ScopeKey
from alya.memory.repository import MAX_CONTENT, MAX_PROVENANCE, fetch_scope, scope_keys_where
from alya.memory.vectors import Vector, decode
from alya.storage.db import Database

log = logging.getLogger(__name__)

_ALIVE = "m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)"
_STATS = ("scopes", "merged", "deduped", "superseded", "contradictions", "pairs_checked",
          "decayed", "purged", "skipped", "llm_errors", "interrupted_before")


@dataclass(frozen=True, slots=True)
class DreamConfig:
    dup_threshold: float = 0.90
    exact_threshold: float = 0.97
    related_threshold: float = 0.60
    max_cluster: int = 8
    max_clusters_per_run: int = 50
    max_pairs_per_run: int = 40
    stale_days: float = 60.0
    decay_step: float = 0.1
    confidence_floor: float = 0.2
    contradiction_factor: float = 0.6
    retention_days: float = 30.0
    protected_confidence: float = 1.0
    merged_confidence_cap: float = 0.95
    keep_runs: int = 200


@dataclass(slots=True)
class DreamReport:
    run_id: int = 0
    scopes: int = 0
    merged: int = 0
    deduped: int = 0
    superseded: int = 0
    contradictions: int = 0
    pairs_checked: int = 0
    decayed: int = 0
    purged: int = 0
    skipped: int = 0
    llm_errors: int = 0
    interrupted_before: int = 0
    touched: set[ScopeKey] = field(default_factory=set)

    def stats(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in _STATS}


class _Stale(Exception):
    """Источники изменились между чтением и записью — кластер пропускаем."""


def _rank(m: Memory) -> tuple[bool, float, bool, int, str]:
    return (m.is_permanent, m.confidence, m.layer == "long", m.usage_count, m.created_at)


def _marks(n: int) -> str:
    return ",".join("?" * n)


class DreamEngine:
    def __init__(
        self, db: Database, judge: DreamJudge | None, *, model_id: str | None = None,
        config: DreamConfig | None = None, clock: Clock = utcnow,
        on_change: Callable[[Iterable[ScopeKey]], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._judge = judge
        self._model_id = model_id
        self._cfg = config or DreamConfig()
        self._clock = clock
        self._on_change = on_change

    @property
    def config(self) -> DreamConfig:
        return self._cfg

    def _now(self) -> str:
        return iso(self._clock())

    def _protected(self, m: Memory) -> bool:
        return m.is_permanent or m.confidence >= self._cfg.protected_confidence

    def _checkpoint(self, conn: sqlite3.Connection, stage: str) -> None:
        """Точка внутри транзакции; тесты переопределяют её, чтобы имитировать падение."""

    # --- журнал прогонов ---------------------------------------------------

    async def recover(self) -> int:
        """Прогоны, оставшиеся в running после падения процесса, помечаются interrupted."""
        now_s = self._now()
        return await self._db.transaction(lambda c: c.execute(
            "UPDATE dream_runs SET status = 'interrupted', finished_at = ? "
            "WHERE status = 'running'", (now_s,)).rowcount)

    async def last_run(self) -> dict[str, Any] | None:
        rows = await self._db.fetchall(
            "SELECT id, reason, status, stats, error, started_at, finished_at "
            "FROM dream_runs ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "reason": r[1], "status": r[2], "stats": json.loads(r[3]),
                "error": r[4], "started_at": r[5], "finished_at": r[6]}

    async def new_memories_since(self, since_iso: str | None) -> int:
        sql = ("SELECT COUNT(*) FROM memories WHERE layer <> 'working' "
               "AND json_extract(provenance, '$.dream') IS NULL")
        params: list[Any] = []
        if since_iso is not None:
            sql += " AND created_at > ?"
            params.append(since_iso)
        rows = await self._db.fetchall(sql, params)
        return int(rows[0][0])

    async def _finish(self, report: DreamReport, status: str, error: str | None) -> None:
        now_s = self._now()
        stats = json.dumps(report.stats(), separators=(",", ":"))
        keep = self._cfg.keep_runs

        def op(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE dream_runs SET status = ?, stats = ?, error = ?, finished_at = ? "
                         "WHERE id = ?", (status, stats, error, now_s, report.run_id))
            conn.execute("DELETE FROM dream_runs WHERE id NOT IN "
                         "(SELECT id FROM dream_runs ORDER BY id DESC LIMIT ?)", (keep,))

        await self._db.transaction(op)

    # --- прогон -------------------------------------------------------------

    async def run(self, reason: str = "manual") -> DreamReport:
        report = DreamReport()
        report.interrupted_before = await self.recover()
        now_s = self._now()
        report.run_id = int(await self._db.transaction(lambda c: c.execute(
            "INSERT INTO dream_runs (reason, status, started_at) VALUES (?, 'running', ?)",
            (reason[:32] or "manual", now_s)).lastrowid))
        try:
            await self._consolidate_all(report)
            await self._decay(report)
            await self._purge(report)
        except Exception as exc:
            await self._finish(report, "failed", type(exc).__name__)
            await self._notify(report)
            raise
        await self._finish(report, "done", None)
        await self._notify(report)
        log.info("dream: прогон %d завершён %s", report.run_id, report.stats())
        return report

    async def _notify(self, report: DreamReport) -> None:
        if self._on_change is None or not report.touched:
            return
        try:
            await self._on_change(sorted(report.touched, key=ScopeKey.sort_key))
        except Exception:
            log.exception("dream: не удалось обновить зеркало")

    async def _consolidate_all(self, report: DreamReport) -> None:
        now_s = self._now()
        keys = await self._db.read(lambda c: scope_keys_where(
            c, _ALIVE + " AND m.layer <> 'working'", [now_s]))
        clusters_left = self._cfg.max_clusters_per_run
        pairs_left = self._cfg.max_pairs_per_run
        for key in sorted(keys, key=ScopeKey.sort_key):
            items = await self._db.read(lambda c, k=key: self._load_items(c, k))
            if len(items) < 2:
                continue
            report.scopes += 1
            pairs = scored_pairs(items)
            clusters = build_clusters(items, pairs, self._cfg.dup_threshold, self._cfg.max_cluster)
            used = {it.memory.id for group in clusters for it in group}
            for group in clusters:
                if clusters_left <= 0:
                    break
                clusters_left -= 1
                await self._consolidate(report, key, group)
            if self._judge is None:
                continue
            candidates = related_pairs(items, pairs, self._cfg.related_threshold,
                                       self._cfg.dup_threshold)
            checked = await self._db.read(lambda c, its=items: self._checked(c, its))
            gone: set[str] = set()
            for a, b in candidates:
                if pairs_left <= 0:
                    break
                ids = (a.memory.id, b.memory.id)
                if used.intersection(ids) or gone.intersection(ids):
                    continue
                if tuple(sorted(ids)) in checked:
                    continue
                if (looks_like_instruction(a.memory.content)
                        or looks_like_instruction(b.memory.content)):
                    continue
                pairs_left -= 1
                gone |= await self._check_pair(report, key, a.memory, b.memory)

    def _load_items(self, conn: sqlite3.Connection, key: ScopeKey) -> list[Item]:
        memories = [m for m in fetch_scope(conn, key, self._now()) if m.layer != "working"]
        vectors: dict[str, Vector] = {}
        if self._model_id is not None and memories:
            ids = [m.id for m in memories]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for mid, dim, blob in conn.execute(
                    f"SELECT memory_id, dim, vector FROM memory_embeddings "
                    f"WHERE model_id = ? AND memory_id IN ({_marks(len(chunk))})",
                    [self._model_id, *chunk],
                ).fetchall():
                    vectors[mid] = decode(blob, int(dim))
        return [Item.of(m, vectors.get(m.id)) for m in memories]

    def _checked(self, conn: sqlite3.Connection, items: Sequence[Item]) -> set[tuple[str, str]]:
        ids = [it.memory.id for it in items]
        out: set[tuple[str, str]] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            for a, b in conn.execute(
                f"SELECT a_id, b_id FROM dream_checked_pairs WHERE a_id IN ({_marks(len(chunk))})",
                chunk,
            ).fetchall():
                out.add((a, b))
        return out

    def _fresh(self, conn: sqlite3.Connection, memories: Sequence[Memory]) -> dict[str, Memory]:
        """Перечитать источники внутри транзакции и убедиться, что они не изменились."""
        from alya.memory.repository import COLUMNS, row_to_memory

        ids = [m.id for m in memories]
        rows = conn.execute(f"SELECT {COLUMNS} FROM memories m WHERE m.id IN ({_marks(len(ids))}) "
                            f"AND {_ALIVE}", [*ids, self._now()]).fetchall()
        fresh = {r[0]: row_to_memory(r) for r in rows}
        for m in memories:
            cur = fresh.get(m.id)
            if cur is None or cur.content != m.content or cur.scope_key != m.scope_key:
                raise _Stale(m.id)
        return fresh

    async def _apply(self, report: DreamReport, op: Callable[[sqlite3.Connection], None]) -> bool:
        try:
            await self._db.transaction(op)
        except _Stale:
            report.skipped += 1
            return False
        return True

    # --- кластеры -------------------------------------------------------------

    def _all_exact(self, items: Sequence[Item]) -> bool:
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if normalize(a.memory.content) == normalize(b.memory.content):
                    continue
                if similarity(a, b) < self._cfg.exact_threshold:
                    return False
        return True

    async def _consolidate(
        self, report: DreamReport, key: ScopeKey, group: Sequence[Item]
    ) -> set[str]:
        clean = [it for it in group if not looks_like_instruction(it.memory.content)]
        report.skipped += len(group) - len(clean)
        if len(clean) < 2:
            return set()
        mems = [it.memory for it in clean]
        protected = [m for m in mems if self._protected(m)]
        if protected:
            keeper = max(protected, key=_rank)
            others = [m for m in mems if not self._protected(m)]
            return await self._supersede_into(report, key, keeper, others, boost=False)
        if self._all_exact(clean):
            keeper = max(mems, key=_rank)
            return await self._supersede_into(report, key, keeper,
                                              [m for m in mems if m.id != keeper.id], boost=True)
        if self._judge is None:
            return set()
        try:
            text = await self._judge.merge([m.content for m in mems])
        except Exception:
            log.warning("dream: ошибка LLM при слиянии", exc_info=True)
            report.llm_errors += 1
            return set()
        if text is None or len(text) > MAX_CONTENT or looks_like_instruction(text):
            report.skipped += 1
            return set()
        return await self._merge_into_new(report, key, mems, text)

    async def _supersede_into(
        self, report: DreamReport, key: ScopeKey, keeper: Memory, others: Sequence[Memory],
        *, boost: bool, extra: Callable[[sqlite3.Connection], None] | None = None,
    ) -> set[str]:
        if not others:
            return set()
        now_s = self._now()
        run_id = report.run_id
        cfg = self._cfg

        def op(conn: sqlite3.Connection) -> None:
            fresh = self._fresh(conn, [keeper, *others])
            k = fresh[keeper.id]
            for m in others:
                conn.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                             (k.id, now_s, m.id))
                conn.execute("INSERT OR IGNORE INTO memory_links (from_id, to_id, relation, "
                             "created_at) VALUES (?, ?, 'derived_from', ?)", (k.id, m.id, now_s))
            self._checkpoint(conn, "superseded")
            if boost and not self._protected(k):
                group = [k, *(fresh[m.id] for m in others)]
                conf = min(cfg.merged_confidence_cap,
                           max(x.confidence for x in group) + 0.05)
                conf = max(conf, k.confidence)
                layer = "long" if any(x.layer == "long" for x in group) else k.layer
                expires = None if layer == "long" or any(x.expires_at is None for x in group) \
                    else max(x.expires_at for x in group if x.expires_at is not None)
                prov = dict(k.provenance)
                prov["dream"] = {"run": run_id, "absorbed": len(others)}
                raw = json.dumps(prov, ensure_ascii=False, separators=(",", ":"))
                if len(raw) > MAX_PROVENANCE:
                    raw = json.dumps(k.provenance, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "UPDATE memories SET confidence = ?, layer = ?, expires_at = ?, "
                    "usage_count = ?, provenance = ?, updated_at = ? WHERE id = ?",
                    (conf, layer, expires, sum(x.usage_count for x in group), raw, now_s, k.id))
            if extra is not None:
                extra(conn)
            self._checkpoint(conn, "commit")

        if not await self._apply(report, op):
            return set()
        report.deduped += len(others)
        report.superseded += len(others)
        report.touched.add(key)
        return {m.id for m in others}

    async def _merge_into_new(self, report: DreamReport, key: ScopeKey,
                              sources: Sequence[Memory], text: str) -> set[str]:
        cfg = self._cfg
        now_s = self._now()
        new_id = new_ulid(int(self._clock().timestamp() * 1000))
        first = sources[0]
        category = Counter(m.category for m in sources).most_common(1)[0][0]
        layer = "long" if any(m.layer == "long" for m in sources) else "short"
        expires = None if layer == "long" or any(m.expires_at is None for m in sources) \
            else max(m.expires_at for m in sources if m.expires_at is not None)
        conf = min(cfg.merged_confidence_cap, max(m.confidence for m in sources) + 0.05)
        used = [m.last_used_at for m in sources if m.last_used_at]
        prov = json.dumps({"dream": report.run_id, "via": "merge",
                           "sources": [m.id for m in sources]}, separators=(",", ":"))

        def op(conn: sqlite3.Connection) -> None:
            self._fresh(conn, sources)
            conn.execute(
                """
                INSERT INTO memories (id, scope, owner_user_id, chat_id, subject_user_id, layer,
                    category, content, provenance, confidence, is_permanent, usage_count,
                    last_used_at, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (new_id, first.scope, first.owner_user_id, first.chat_id, first.subject_user_id,
                 layer, category, text.strip(), prov, conf, sum(m.usage_count for m in sources),
                 max(used) if used else None, min(m.created_at for m in sources), now_s, expires),
            )
            self._checkpoint(conn, "inserted")
            for m in sources:
                conn.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                             (new_id, now_s, m.id))
                conn.execute("INSERT OR IGNORE INTO memory_links (from_id, to_id, relation, "
                             "created_at) VALUES (?, ?, 'derived_from', ?)", (new_id, m.id, now_s))
            self._checkpoint(conn, "commit")

        if not await self._apply(report, op):
            return set()
        report.merged += 1
        report.superseded += len(sources)
        report.touched.add(key)
        return {m.id for m in sources}

    # --- пары ------------------------------------------------------------------

    def _record_pair(self, conn: sqlite3.Connection, a: Memory, b: Memory,
                     verdict: Relation, now_s: str) -> None:
        lo, hi = sorted((a.id, b.id))
        conn.execute("INSERT OR REPLACE INTO dream_checked_pairs (a_id, b_id, verdict, checked_at) "
                     "VALUES (?, ?, ?, ?)", (lo, hi, verdict, now_s))

    async def _check_pair(
        self, report: DreamReport, key: ScopeKey, a: Memory, b: Memory
    ) -> set[str]:
        assert self._judge is not None
        try:
            verdict = await self._judge.relation(a.content, b.content)
        except Exception:
            log.warning("dream: ошибка LLM при сравнении", exc_info=True)
            report.llm_errors += 1
            return set()
        if verdict is None:
            return set()
        report.pairs_checked += 1
        now_s = self._now()
        if verdict == "same":
            keeper = max((a, b), key=_rank)
            other = b if keeper is a else a
            if self._protected(other):
                verdict = "unrelated"  # обе подтверждены — оставляем как есть
            else:
                return await self._supersede_into(
                    report, key, keeper, [other], boost=True,
                    extra=lambda c: self._record_pair(c, a, b, "same", now_s))
        cfg = self._cfg
        state = {"contradiction": False}

        def op(conn: sqlite3.Connection) -> None:
            fresh = self._fresh(conn, [a, b])
            if verdict == "contradicts":
                newer, older = sorted((fresh[a.id], fresh[b.id]),
                                      key=lambda m: (m.created_at, m.id), reverse=True)
                target = older if not self._protected(older) else (
                    newer if not self._protected(newer) else None)
                if target is not None:
                    conf = max(cfg.confidence_floor,
                               round(target.confidence * cfg.contradiction_factor, 4))
                    conn.execute("UPDATE memories SET confidence = ?, updated_at = ? WHERE id = ?",
                                 (min(conf, target.confidence), now_s, target.id))
                conn.execute("INSERT OR IGNORE INTO memory_links (from_id, to_id, relation, "
                             "created_at) VALUES (?, ?, 'contradicts', ?)",
                             (newer.id, older.id, now_s))
                state["contradiction"] = True
            self._record_pair(conn, a, b, verdict, now_s)
            self._checkpoint(conn, "commit")

        if await self._apply(report, op) and state["contradiction"]:
            report.contradictions += 1
            report.touched.add(key)
        return set()

    # --- устаревание и очистка ---------------------------------------------

    async def _decay(self, report: DreamReport) -> None:
        cfg = self._cfg
        now = self._clock()
        now_s = iso(now)
        cutoff = iso(now - timedelta(days=cfg.stale_days))
        where = ("m.layer = 'long' AND m.is_permanent = 0 AND " + _ALIVE +
                 " AND m.confidence < ? AND m.confidence > ?"
                 " AND COALESCE(m.last_used_at, m.created_at) < ? AND m.updated_at < ?")
        params = [now_s, cfg.protected_confidence, cfg.confidence_floor, cutoff, cutoff]

        def op(conn: sqlite3.Connection) -> tuple[int, list[ScopeKey]]:
            keys = scope_keys_where(conn, where, params)
            n = conn.execute(
                f"UPDATE memories AS m SET confidence = MAX(?, m.confidence - ?), updated_at = ? "
                f"WHERE {where}", [cfg.confidence_floor, cfg.decay_step, now_s, *params]).rowcount
            return n, keys

        n, keys = await self._db.transaction(op)
        report.decayed += n
        if n:
            report.touched.update(keys)

    async def _purge(self, report: DreamReport) -> None:
        cutoff = iso(self._clock() - timedelta(days=self._cfg.retention_days))
        report.purged += await self._db.transaction(lambda c: c.execute(
            "DELETE FROM memories WHERE superseded_by IS NOT NULL AND is_permanent = 0 "
            "AND updated_at < ?", (cutoff,)).rowcount)
