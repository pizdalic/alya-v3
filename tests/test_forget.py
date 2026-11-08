import sqlite3
import unittest
from pathlib import Path

from alya.memory.forget import list_tombstones, replay_tombstones
from alya.memory.indexer import EmbeddingIndexer
from alya.memory.models import MemoryContext
from alya.storage.db import Database
from tests.memory_helpers import GROUP, A, B, MemoryTestCase

A_DM = MemoryContext.private(A)
A_G = MemoryContext.group(GROUP, A)
B_G = MemoryContext.group(GROUP, B)
B_DM = MemoryContext.private(B)


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:  # type: ignore[type-arg]
    return int(conn.execute(sql, params).fetchone()[0])


class ForgetTests(MemoryTestCase):
    use_embedder = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = self.repo.now_iso()
        self.a1 = await self.service.remember(A_DM, "любимый цвет — синий", scope="private")
        self.a2 = await self.service.remember(A_G, "A — дизайнер, синий логотип", scope="chat",
                                              subject_user_id=A)
        self.b1 = await self.service.remember(B_DM, "любимый цвет — синий тоже", scope="private")
        self.bg = await self.service.remember(B_G, "B ведёт этот чат", scope="chat",
                                              subject_user_id=B)
        assert self.embedder is not None
        await EmbeddingIndexer(self.db, self.embedder, clock=self.clock).embed_pending()
        await self.db.transaction(lambda c: c.executemany(
            "INSERT INTO messages (chat_id, tg_message_id, sender_user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(A, 1, A, "синий", now), (A, 2, None, "ответ Али", now),
             (GROUP, 3, A, "синий", now), (GROUP, 4, B, "привет", now)]))
        await self.db.transaction(lambda c: c.execute(
            "INSERT INTO outbox (chat_id, idempotency_key, payload, next_attempt_at, created_at, "
            "updated_at) VALUES (?, 'k1', '{}', ?, ?, ?)", (A, now, now, now)))

    def assert_user_gone(self, conn: sqlite3.Connection, uid: int) -> None:
        checks = {
            "users": ("SELECT COUNT(*) FROM users WHERE user_id = ?", (uid,)),
            "private chat": ("SELECT COUNT(*) FROM chats WHERE chat_id = ?", (uid,)),
            "memberships": ("SELECT COUNT(*) FROM chat_members WHERE user_id = ?", (uid,)),
            "memories": ("SELECT COUNT(*) FROM memories WHERE owner_user_id = ? OR "
                         "subject_user_id = ? OR chat_id = ?", (uid, uid, uid)),
            "messages": ("SELECT COUNT(*) FROM messages WHERE sender_user_id = ? OR chat_id = ?",
                         (uid, uid)),
            "outbox": ("SELECT COUNT(*) FROM outbox WHERE chat_id = ?", (uid,)),
            "fts": ("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'синий' "
                    "AND rowid NOT IN (SELECT seq FROM memories)", ()),
            "embeddings": ("SELECT COUNT(*) FROM memory_embeddings WHERE memory_id NOT IN "
                           "(SELECT id FROM memories)", ()),
        }
        for store, (sql, params) in checks.items():
            with self.subTest(store=store):
                self.assertEqual(count(conn, sql, params), 0)
        self.assertEqual(
            conn.execute("INSERT INTO memories_fts(memories_fts, rank) "
                         "VALUES('integrity-check', 1)").fetchall(), [])

    async def test_forget_user_every_store(self) -> None:
        self.assertTrue(self.mirror.path_for(self.a1.scope_key).exists())
        report = await self.service.forget_user(A, requested_by=A)
        self.assertEqual(report.memories, 2)
        self.assertEqual(report.messages, 3)
        self.assert_user_gone(self.db.conn, A)
        # чужое не задето
        self.assertIsNotNone(await self.repo.get(B_DM, self.b1.id))
        self.assertIsNotNone(await self.repo.get(B_G, self.bg.id))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM memory_embeddings"), 2)
        # FTS по-прежнему находит чужое и не находит удалённое
        res = await self.search.search(B_G, "синий логотип дизайнер")
        self.assertNotIn(self.a2.id, {h.memory.id for h in res.hits})
        # зеркало
        self.assertFalse(self.mirror.path_for(self.a1.scope_key).exists())
        group_md = self.mirror.path_for(self.bg.scope_key).read_text(encoding="utf-8")
        self.assertNotIn("дизайнер", group_md)
        self.assertIn("ведёт этот чат", group_md)
        for path in self.mirror.files():
            self.assertNotIn("любимый цвет — синий\n", path.read_text(encoding="utf-8"))
        # журнал без содержимого
        rows = self.db.conn.execute("SELECT * FROM deletion_journal").fetchall()
        self.assertEqual([(r["target_type"], r["target_id"]) for r in rows], [("user", str(A))])

    async def test_forget_chat(self) -> None:
        report = await self.service.forget_chat(GROUP, requested_by=None)
        self.assertEqual((report.memories, report.messages), (2, 2))
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM chats WHERE chat_id = ?",
                               (GROUP,)), 0)
        self.assertFalse(self.mirror.path_for(self.bg.scope_key).exists())
        self.assertIsNotNone(await self.repo.get(A_DM, self.a1.id))

    async def test_backup_forget_restore_replay(self) -> None:
        backup_path = Path(self.tmp.name) / "backup.sqlite3"
        dst = sqlite3.connect(backup_path)
        self.db.conn.backup(dst)
        dst.close()
        await self.service.forget_user(A, requested_by=A)
        await self.service.forget_memory(B_DM, self.b1.id)
        tombstones = await self.db.read(list_tombstones)
        self.assertEqual(len(tombstones), 2)

        restored = Database(backup_path)
        restored.connect()
        try:
            self.assertGreater(count(restored.conn, "SELECT COUNT(*) FROM memories "
                                     "WHERE owner_user_id = ?", (A,)), 0)
            removed = await restored.transaction(lambda c: replay_tombstones(c, tombstones))
            self.assertGreater(removed, 0)
            self.assert_user_gone(restored.conn, A)
            self.assertEqual(count(restored.conn, "SELECT COUNT(*) FROM memories WHERE id = ?",
                                   (self.b1.id,)), 0)
            self.assertEqual(count(restored.conn, "SELECT COUNT(*) FROM deletion_journal"), 2)
            again = await restored.transaction(lambda c: replay_tombstones(c, tombstones))
            self.assertEqual(again, 0)
        finally:
            restored.close()


if __name__ == "__main__":
    unittest.main()
