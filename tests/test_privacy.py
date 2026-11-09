import sqlite3
from datetime import timedelta

from alya.memory.models import MemoryContext
from alya.telegram.addressee import AddresseeDetector
from alya.telegram.outbox import OutboxStore
from alya.telegram.pipeline import Pipeline, PipelineConfig
from alya.telegram.privacy import PrivacyCommands
from alya.telegram.sender import Sender
from tests.memory_helpers import GROUP, A, B, C, MemoryTestCase
from tests.telegram_helpers import FakeEngine, FakeTransport, Sleeps, msg

OWNER = A
ME = 555
STRANGER = 909


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:  # type: ignore[type-arg]
    return int(conn.execute(sql, params).fetchone()[0])


class PrivacyTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pc = PrivacyCommands(self.db, self.service, owner_id=OWNER, clock=self.clock,
                                  per_hour=5)
        self.b1 = await self.service.remember(MemoryContext.private(B), "любит чай с мятой",
                                              scope="private")
        self.bg = await self.service.remember(MemoryContext.group(GROUP, B), "B дизайнер",
                                              scope="chat", subject_user_id=B)
        self.c1 = await self.service.remember(MemoryContext.private(C), "секрет C",
                                              scope="private")
        now = self.repo.now_iso()
        await self.db.transaction(lambda c: c.executemany(
            "INSERT INTO messages (chat_id, tg_message_id, sender_user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)", [(B, 1, B, "привет", now), (GROUP, 2, B, "хай", now),
                                      (C, 3, C, "тайна", now)]))

    def dm(self, sender: int, text: str):  # type: ignore[no-untyped-def]
        return msg(sender, sender, text, at=self.clock(), kind="private")

    async def ask(self, sender: int, text: str) -> str:
        return "\n".join(await self.pc.handle(self.dm(sender, text)))

    def mid(self, record) -> str:  # type: ignore[no-untyped-def]
        return str(getattr(record, "id", record))

    async def test_accepts_only_private_commands(self) -> None:
        self.assertTrue(self.pc.accepts(self.dm(B, "/mydata")))
        self.assertTrue(self.pc.accepts(self.dm(B, "/mydata@AlysiaNya")))
        self.assertFalse(self.pc.accepts(self.dm(B, "привет")))
        self.assertFalse(self.pc.accepts(msg(GROUP, B, "/mydata", at=self.clock())))
        self.assertFalse(self.pc.accepts(self.dm(B, "/forget_user 303 да")))
        self.assertTrue(self.pc.accepts(self.dm(OWNER, "/forget_user 303 да")))

    async def test_mydata_is_isolated(self) -> None:
        text = await self.ask(B, "/mydata")
        self.assertIn("чай с мятой", text)
        self.assertIn("B дизайнер", text)
        self.assertNotIn("секрет C", text)
        self.assertIn("записей о тебе: 2", text)
        self.assertIn("сообщений в истории: 2", text)
        empty = await self.ask(STRANGER, "/mydata")
        self.assertIn("ничего не храню", empty)

    async def test_export_lists_all_and_splits(self) -> None:
        for i in range(60):
            await self.service.remember(MemoryContext.private(B), f"факт {i} " + "слово " * 30,
                                        scope="private")
        parts = await self.pc.handle(self.dm(B, "/export"))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        joined = "\n".join(parts)
        self.assertIn("факт 59", joined)
        self.assertNotIn("секрет C", joined)

    async def test_forget_one_by_prefix(self) -> None:
        mid = self.mid(self.b1)
        self.assertIn("забыла", await self.ask(B, f"/forget {mid[:10]}"))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM memories WHERE id = ?",
                               (mid,)), 0)
        # групповая запись о себе тоже можно забыть
        self.assertIn("забыла", await self.ask(B, f"/forget {self.mid(self.bg)}"))
        # чужую — нельзя, и даже не видно, что она есть
        self.assertIn("нет", await self.ask(B, f"/forget {self.mid(self.c1)}"))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM memories WHERE id = ?",
                               (self.mid(self.c1),)), 1)
        self.assertIn("формат", await self.ask(B, "/forget ab"))
        self.assertIn("нет", await self.ask(B, "/forget %%%%%%%%"))

    async def test_forget_me_requires_confirmation(self) -> None:
        warn = await self.ask(B, "/forget_me")
        self.assertIn("/forget_me да", warn)
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM users WHERE user_id = ?",
                               (B,)), 1)
        done = await self.ask(B, "/forget_me да")
        self.assertIn("забыла", done)
        conn = self.db.conn
        for sql, params in {
            "SELECT COUNT(*) FROM users WHERE user_id = ?": (B,),
            "SELECT COUNT(*) FROM memories WHERE owner_user_id = ? OR subject_user_id = ? "
            "OR chat_id = ?": (B, B, B),
            "SELECT COUNT(*) FROM messages WHERE sender_user_id = ? OR chat_id = ?": (B, B),
            "SELECT COUNT(*) FROM relationships WHERE user_id = ?": (B,),
        }.items():
            with self.subTest(sql=sql):
                self.assertEqual(count(conn, sql, params), 0)
        self.assertEqual(count(conn, "SELECT COUNT(*) FROM memories WHERE id = ?",
                               (self.mid(self.c1),)), 1)
        self.assertGreater(count(conn, "SELECT COUNT(*) FROM deletion_journal"), 0)

    async def test_owner_commands(self) -> None:
        preview = await self.ask(OWNER, f"/forget_user {C}")
        self.assertIn("подтверди", preview)
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM users WHERE user_id = ?",
                               (C,)), 1)
        self.assertEqual(await self.pc.handle(self.dm(B, f"/forget_user {C} да")), [])
        self.assertIn("удалено", await self.ask(OWNER, f"/forget_user {C} да"))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM users WHERE user_id = ?",
                               (C,)), 0)
        self.assertIn("удалено", await self.ask(OWNER, f"/forget_chat {GROUP} да"))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                               (GROUP,)), 0)
        self.assertIn("/forget_user", await self.ask(OWNER, "/privacy"))
        self.assertNotIn("/forget_user", await self.ask(B, "/privacy"))

    async def test_rate_limit(self) -> None:
        for _ in range(5):
            self.assertTrue(await self.pc.handle(self.dm(B, "/privacy")))
        self.assertIn("час", await self.ask(B, "/privacy"))
        self.assertEqual(await self.pc.handle(self.dm(B, "/privacy")), [])
        self.clock.advance(timedelta(hours=1, seconds=1))
        self.assertTrue(await self.pc.handle(self.dm(B, "/privacy")))
        for _ in range(20):  # владелец без лимита
            self.assertTrue(await self.pc.handle(self.dm(OWNER, "/privacy")))


class PrivacyPipelineTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.engine = FakeEngine()
        self.tr = FakeTransport()
        det = AddresseeDetector(ME, "AlysiaNya", ["аля"], clock=self.clock)
        self.outbox = OutboxStore(self.db, clock=self.clock)
        privacy = PrivacyCommands(self.db, self.service, owner_id=OWNER, clock=self.clock)
        self.p = Pipeline(self.db, self.engine, det, self.outbox, transport=self.tr,
                          clock=self.clock, privacy=privacy,
                          config=PipelineConfig(me_id=ME, owner_id=OWNER, strangers="owner_only",
                                                debounce_s=0.01, debounce_max_wait_s=0.05))
        self.sender = Sender(self.outbox, self.tr, on_sent=self.p.on_sent, clock=self.clock,
                             chat_interval_s=0, global_interval_s=0, sleep=Sleeps())

    async def feed(self, m) -> None:  # type: ignore[no-untyped-def]
        await self.p.on_message(m)
        await self.p.drain()
        while await self.sender.run_once():
            pass

    async def test_stranger_can_use_privacy_without_llm_or_storage(self) -> None:
        await self.feed(msg(STRANGER, STRANGER, "/mydata", at=self.clock(), kind="private"))
        self.assertEqual(len(self.tr.sent), 1)
        self.assertIn("ничего не храню", self.tr.sent[0][1])
        self.assertEqual(self.engine.turns, [])
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                               (STRANGER,)), 0)
        # обычное сообщение незнакомца по-прежнему игнорируется
        await self.feed(msg(STRANGER, STRANGER, "привет", at=self.clock(), kind="private"))
        self.assertEqual(len(self.tr.sent), 1)
        self.assertEqual(self.engine.turns, [])
