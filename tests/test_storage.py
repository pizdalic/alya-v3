import sqlite3
import tempfile
import unittest
from pathlib import Path

from alya.core.errors import MigrationError
from alya.storage.db import Database
from alya.storage.migrator import Migration, apply_migrations, load_migrations

TABLES = {"users", "chats", "chat_members", "messages", "outbox", "audit_log",
          "deletion_journal", "schema_migrations"}


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "sub" / "t.sqlite3")
        self.db.connect()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_pragmas(self) -> None:
        c = self.db.conn
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertTrue(self.db.fts5_available())

    def test_apply_idempotent(self) -> None:
        self.assertEqual(apply_migrations(self.db.conn), [1, 2, 3, 4, 5, 6])
        self.assertTrue(TABLES <= tables(self.db.conn))
        self.assertEqual(apply_migrations(self.db.conn), [])

    def test_checksum_mismatch(self) -> None:
        apply_migrations(self.db.conn)
        changed = [Migration(1, "init", load_migrations()[0].sql + "\n-- edit")]
        with self.assertRaises(MigrationError):
            apply_migrations(self.db.conn, changed)

    def test_db_newer_than_code(self) -> None:
        ms = [Migration(1, "a", "CREATE TABLE a (x)"), Migration(2, "b", "CREATE TABLE b (x)")]
        apply_migrations(self.db.conn, ms)
        with self.assertRaises(MigrationError):
            apply_migrations(self.db.conn, ms[:1])

    def test_failed_migration_is_atomic(self) -> None:
        ms = [Migration(1, "bad", "CREATE TABLE half (x);\nCREATE TABLE half (y);")]
        with self.assertRaises(MigrationError):
            apply_migrations(self.db.conn, ms)
        self.assertNotIn("half", tables(self.db.conn))
        self.assertFalse(self.db.conn.in_transaction)
        count = self.db.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(count, 0)

    def test_constraints(self) -> None:
        apply_migrations(self.db.conn)
        c = self.db.conn
        with self.assertRaises(sqlite3.IntegrityError):  # внешний ключ
            c.execute("INSERT INTO messages (chat_id, tg_message_id, created_at) VALUES (1, 1, 't')")
        with self.assertRaises(sqlite3.IntegrityError):  # json_valid
            c.execute("INSERT INTO outbox (chat_id, idempotency_key, payload, next_attempt_at, "
                      "created_at, updated_at) VALUES (1, 'k', 'not json', 't', 't', 't')")


class LoaderTests(unittest.TestCase):
    def test_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "0001_a.sql").write_text("CREATE TABLE a (x);")
            (d / "0003_c.sql").write_text("CREATE TABLE c (x);")
            with self.assertRaises(MigrationError):  # дыра в нумерации
                load_migrations(d)
            (d / "0003_c.sql").unlink()
            (d / "0002_b.sql").write_text("BEGIN;\nCREATE TABLE b (x);\nCOMMIT;")
            with self.assertRaises(MigrationError):  # своя транзакция
                load_migrations(d)
            (d / "0002_b.sql").write_text("CREATE TABLE b (x);")
            (d / "README.txt").write_text("ignored")
            self.assertEqual([m.version for m in load_migrations(d)], [1, 2])


class AsyncDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_transaction_rollback(self) -> None:
        db = Database(":memory:")
        db.connect()
        await db.execute("CREATE TABLE t (x INTEGER)")

        def boom(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("fail")

        with self.assertRaises(RuntimeError):
            await db.transaction(boom)
        self.assertEqual((await db.fetchall("SELECT COUNT(*) FROM t"))[0][0], 0)
        self.assertEqual(await db.execute("INSERT INTO t VALUES (?)", (2,)), 1)
        self.assertEqual((await db.fetchall("SELECT x FROM t"))[0]["x"], 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
