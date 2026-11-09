import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alya.memory.models import MemoryContext
from alya.storage.backup import (BackupError, BackupScheduler, create_backup, export_user,
                                 list_backups, restore_backup, rotate)
from alya.storage.backup import SAFETY
from tests.memory_helpers import A, B, MemoryTestCase

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def count(path: Path, sql: str, params: tuple = ()) -> int:  # type: ignore[type-arg]
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


class BackupTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "t.sqlite3"
        self.dest = self.root / "backups"
        await self.service.remember(MemoryContext.private(A), "A любит чай", scope="private")
        await self.service.remember(MemoryContext.private(B), "B любит кофе", scope="private")

    def mem(self, uid: int) -> int:
        return count(self.db_path, "SELECT COUNT(*) FROM memories WHERE owner_user_id = ?", (uid,))

    async def test_backup_forget_restore_does_not_resurrect(self) -> None:
        backup = create_backup(self.db_path, self.dest, now=NOW)
        self.assertEqual(count(backup, "SELECT COUNT(*) FROM memories"), 2)
        await self.service.forget_user(A, requested_by=A)
        self.assertEqual(self.mem(A), 0)
        rep = restore_backup(backup, self.db_path, now=NOW + timedelta(hours=1))
        self.assertEqual(self.mem(A), 0, "забытое вернулось из бэкапа")
        self.assertEqual(self.mem(B), 1)
        self.assertGreaterEqual(rep.tombstones, 1)
        self.assertGreaterEqual(rep.removed, 1)
        assert rep.safety_copy is not None
        self.assertTrue(rep.safety_copy.exists())
        self.assertEqual(count(self.db_path, "SELECT COUNT(*) FROM users WHERE user_id = ?", (A,)), 0)
        self.assertGreaterEqual(count(self.db_path, "SELECT COUNT(*) FROM deletion_journal"), 1)

    def test_rotation_keeps_newest_by_age_and_count(self) -> None:
        for days in (10, 3, 2, 1, 0):
            create_backup(self.db_path, self.dest, keep=100, retain_days=100,
                          now=NOW - timedelta(days=days))
        self.assertEqual(len(list_backups(self.dest)), 5)
        removed = rotate(self.dest, keep=3, retain_days=7, now=NOW)
        self.assertEqual(len(removed), 2)
        left = list_backups(self.dest)
        self.assertEqual([p.name for p in left],
                         [f"alya-{(NOW - timedelta(days=d)):%Y%m%d-%H%M%S}.sqlite3" for d in (2, 1, 0)])
        rotate(self.dest, keep=1, retain_days=1, now=NOW + timedelta(days=30))
        self.assertEqual(len(list_backups(self.dest)), 1, "самый свежий не удаляется")
        self.assertFalse(list(self.dest.glob("*.part")))

    def test_bad_inputs(self) -> None:
        with self.assertRaises(BackupError):
            create_backup(self.root / "missing.sqlite3", self.dest)
        junk = self.root / "junk.sqlite3"
        junk.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(BackupError):
            restore_backup(junk, self.db_path)
        self.assertEqual(self.mem(A), 1, "рабочая БД не тронута")
        with self.assertRaises(BackupError):
            restore_backup(self.root / "nope.sqlite3", self.db_path)

    def test_export_user_is_scoped_and_serializable(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            data = export_user(conn, A)
        finally:
            conn.close()
        text = json.dumps(data, ensure_ascii=False, default=str)
        self.assertIn("чай", text)
        self.assertNotIn("кофе", text)
        self.assertEqual(len(data["memories"]), 1)
        self.assertEqual(data["user"][0]["user_id"], A)

    async def test_scheduler_run_once(self) -> None:
        ok = BackupScheduler(self.db_path, self.dest, interval_h=24, keep=7, retain_days=7)
        path = await ok.run_once()
        assert path is not None
        self.assertTrue(path.exists())
        bad = BackupScheduler(self.root / "missing.sqlite3", self.dest, interval_h=24, keep=7,
                              retain_days=7)
        with self.assertLogs("alya.storage.backup", "ERROR"):
            self.assertIsNone(await bad.run_once())
        await ok.start()
        await ok.stop()

    def test_safety_copies_expire(self) -> None:
        old = self.db_path.with_name(f"{self.db_path.name}{SAFETY}20261001-000000")
        fresh = self.db_path.with_name(f"{self.db_path.name}{SAFETY}20261108-000000")
        for f in (old, fresh):
            f.write_bytes(b"x")
        create_backup(self.db_path, self.dest, retain_days=7, now=NOW)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_cli_parser_has_backup_commands(self) -> None:
        from alya.cli import build_parser
        p = build_parser()
        for argv in (["backup"], ["backups"], ["restore", "x.sqlite3", "--yes"],
                     ["export-user", "1", "--out", "o.json"]):
            with self.subTest(argv=argv):
                self.assertTrue(callable(p.parse_args(argv).fn))
