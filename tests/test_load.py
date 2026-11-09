"""Нагрузочные тесты: сотни сообщений, тысячи отправителей, большая БД (без сети)."""

import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from alya.storage.backup import create_backup
from alya.telegram import moderation
from alya.telegram.addressee import AddresseeDetector
from alya.telegram.moderation import ModerationRules, Moderator
from alya.telegram.outbox import OutboxStore
from alya.telegram.pipeline import Pipeline, PipelineConfig
from alya.telegram.sender import Sender
from tests.memory_helpers import A, MemoryTestCase
from tests.telegram_helpers import DbTestCase, FakeEngine, FakeTransport, Sleeps, msg

ME, OWNER, G = 555, 101, -1001


class PipelineLoadTests(DbTestCase):
    async def build(self, **cfg) -> None:  # type: ignore[no-untyped-def]
        self.engine = FakeEngine()
        self.tr = FakeTransport()
        det = AddresseeDetector(ME, "AlysiaNya", ["аля"], clock=self.clock)
        self.outbox = OutboxStore(self.db, clock=self.clock)
        config = PipelineConfig(me_id=ME, owner_id=OWNER, debounce_s=0.01,
                                debounce_max_wait_s=0.05, **cfg)
        self.p = Pipeline(self.db, self.engine, det, self.outbox, config=config,
                          transport=self.tr, clock=self.clock)
        self.sender = Sender(self.outbox, self.tr, on_sent=self.p.on_sent, clock=self.clock,
                             chat_interval_s=0, global_interval_s=0, sleep=Sleeps())

    async def flush(self) -> None:
        await self.p.drain()
        while await self.sender.run_once():
            pass

    async def count(self, sql: str, params: tuple = ()) -> int:  # type: ignore[type-arg]
        rows = await self.db.fetchall(sql, params)
        return int(rows[0][0])

    async def test_group_chatter_is_context_only(self) -> None:
        await self.build()
        for i in range(300):
            await self.p.on_message(msg(G, 1000 + i % 30, f"болтовня {i}", at=self.clock()))
        await self.flush()
        self.assertEqual(len(self.engine.turns), 0)
        self.assertEqual(await self.count("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND "
                                          "context_only = 1", (G,)), 300)
        self.assertEqual(self.tr.sent, [])

    async def test_burst_is_debounced_and_delivered_once(self) -> None:
        await self.build(replies_per_10min=1000)
        for i in range(100):
            await self.p.on_message(msg(OWNER, OWNER, f"m{i}", at=self.clock(), kind="private"))
        await self.flush()
        turns = self.engine.turns
        self.assertLess(len(turns), 100, "debounce не склеил поток")
        lines = [line for t in turns for line in t.text.split("\n")]
        self.assertEqual(sorted(lines), sorted(f"m{i}" for i in range(100)), "потеря или дубль")
        self.assertEqual(len(self.tr.sent), len(turns), "каждый ответ ровно один раз")
        self.assertEqual(await self.count("SELECT COUNT(*) FROM outbox WHERE status != 'sent'"), 0)

    async def test_reply_rate_limit_caps_cost(self) -> None:
        await self.build(replies_per_10min=20)
        for i in range(60):
            await self.p.on_message(msg(OWNER, OWNER, f"вопрос {i}", at=self.clock(),
                                        kind="private"))
            await self.flush()
        self.assertEqual(len(self.engine.turns), 20, "вызовов LLM не больше лимита")
        self.clock.advance(timedelta(minutes=11))
        await self.p.on_message(msg(OWNER, OWNER, "ещё", at=self.clock(), kind="private"))
        await self.flush()
        self.assertEqual(len(self.engine.turns), 21, "окно лимита освободилось")


class ModerationLoadTests(DbTestCase):
    async def test_memory_bounded_with_many_senders(self) -> None:
        mod = Moderator(self.db, ModerationRules(), clock=self.clock)
        t0 = self.clock()
        for i in range(moderation.MAX_TRACKED + 3000):
            mod.evaluate(msg(G, 10_000 + i, "привет", at=t0 + timedelta(milliseconds=50 * i)))
            self.assertLessEqual(len(mod._recent), moderation.MAX_TRACKED)
        self.assertLessEqual(len(mod._flagged), moderation.MAX_TRACKED)

    async def test_flood_detected_under_load(self) -> None:
        mod = Moderator(self.db, ModerationRules(flood_messages=8, flood_window_s=30),
                        clock=self.clock)
        t0 = self.clock()
        flagged = 0
        for i in range(500):
            if await mod.check(msg(G, 7, f"спам {i}", at=t0 + timedelta(seconds=i))):
                flagged += 1
        self.assertTrue(1 <= flagged <= 500 // 30 + 2, flagged)


class BackupLoadTests(MemoryTestCase):
    async def test_large_db_backup(self) -> None:
        now = self.repo.now_iso()
        n = 20_000
        await self.db.transaction(lambda c: c.executemany(
            "INSERT INTO messages (chat_id, tg_message_id, sender_user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)", [(A, i, A, "текст " * 20, now) for i in range(n)]))
        root = Path(self.tmp.name)
        started = time.monotonic()
        path = create_backup(root / "t.sqlite3", root / "backups")
        self.assertLess(time.monotonic() - started, 30)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], n)
        finally:
            conn.close()
