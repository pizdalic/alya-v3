"""Резервные копии (ADR-0010): online backup API SQLite, integrity_check, ротация по сроку,
restore с повторным применением журнала удалений и выгрузка данных одного человека.

restore делается на ОСТАНОВЛЕННОЙ Але: журнал удалений берётся из текущей БД (он
новее бэкапа), текущая БД сначала копируется в *.pre-restore-*, потом заменяется бэкапом, и
все удаления из журнала применяются заново — забытое не «воскресает».
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alya.core.errors import AlyaError
from alya.core.timeutil import utcnow
from alya.memory.forget import Tombstone, replay_tombstones

log = logging.getLogger(__name__)

PREFIX = "alya-"
SUFFIX = ".sqlite3"
STAMP = "%Y%m%d-%H%M%S"


class BackupError(AlyaError):
    pass


@dataclass(frozen=True, slots=True)
class RestoreReport:
    restored_from: Path
    safety_copy: Path | None
    tombstones: int
    removed: int


def _integrity(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name}: не БД SQLite ({exc})") from None
    finally:
        conn.close()
    if res != "ok":
        raise BackupError(f"{path.name}: integrity_check: {res}")


def _copy(src_path: Path, dst_path: Path, *, readonly_src: bool = False) -> None:
    src = (sqlite3.connect(f"file:{src_path}?mode=ro", uri=True) if readonly_src
           else sqlite3.connect(src_path))
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _stamp_of(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name[len(PREFIX):-len(SUFFIX)], STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def list_backups(dest: Path) -> list[Path]:
    """Бэкапы от старых к новым (только файлы с корректной меткой времени)."""
    if not dest.is_dir():
        return []
    found = [p for p in dest.glob(f"{PREFIX}*{SUFFIX}") if _stamp_of(p) is not None]
    return sorted(found, key=lambda p: _stamp_of(p) or datetime.min.replace(tzinfo=UTC))


def rotate(dest: Path, *, keep: int, retain_days: int, now: datetime | None = None) -> list[Path]:
    """Удалить старше retain_days и сверх keep. Самый свежий бэкап не удаляется никогда."""
    files = list_backups(dest)
    if not files:
        return []
    limit = (now or utcnow()) - timedelta(days=retain_days)
    newest = files[-1]
    excess = set(files[:-max(keep, 1)])
    removed: list[Path] = []
    for p in files:
        if p is newest:
            continue
        stamp = _stamp_of(p)
        if p in excess or (stamp is not None and stamp < limit):
            p.unlink(missing_ok=True)
            removed.append(p)
    return removed


def create_backup(db_path: Path, dest: Path, *, keep: int = 7, retain_days: int = 7,
                  now: datetime | None = None) -> Path:
    if not db_path.exists():
        raise BackupError(f"БД не найдена: {db_path}")
    dest.mkdir(parents=True, exist_ok=True)
    moment = now or utcnow()
    final = dest / f"{PREFIX}{moment.strftime(STAMP)}{SUFFIX}"
    part = final.with_name(final.name + ".part")
    part.unlink(missing_ok=True)
    _copy(db_path, part)
    try:
        _integrity(part)
    except BackupError:
        part.unlink(missing_ok=True)
        raise
    part.chmod(0o600)
    part.replace(final)
    rotate(dest, keep=keep, retain_days=retain_days, now=moment)
    return final


def read_tombstones(conn: sqlite3.Connection) -> list[Tombstone]:
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                       "name = 'deletion_journal'").fetchone()
    if not has:
        return []
    rows = conn.execute("SELECT target_type, target_id, created_at FROM deletion_journal "
                        "WHERE target_type IN ('user', 'chat', 'memory') ORDER BY id").fetchall()
    return [Tombstone(r[0], str(r[1]), str(r[2])) for r in rows]


def restore_backup(backup: Path, db_path: Path, *, now: datetime | None = None) -> RestoreReport:
    if not backup.exists():
        raise BackupError(f"бэкап не найден: {backup}")
    _integrity(backup)
    tombstones: list[Tombstone] = []
    safety: Path | None = None
    if db_path.exists():
        cur = sqlite3.connect(db_path)
        try:
            tombstones = read_tombstones(cur)
        finally:
            cur.close()
        safety = db_path.with_name(f"{db_path.name}.pre-restore-{(now or utcnow()).strftime(STAMP)}")
        _copy(db_path, safety)
        safety.chmod(0o600)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    _copy(backup, db_path, readonly_src=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed = replay_tombstones(conn, tombstones)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    _integrity(db_path)
    return RestoreReport(backup, safety, len(tombstones), removed)


def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description or ()]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def export_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Всё, что хранится о человеке (для «попроси владельца выгрузить файлом»). Без эмбеддингов."""
    uid = int(user_id)
    q = conn.execute
    return {
        "user_id": uid,
        "exported_at": utcnow().isoformat(),
        "user": _dicts(q("SELECT * FROM users WHERE user_id = ?", (uid,))),
        "memories": _dicts(q(
            "SELECT * FROM memories WHERE owner_user_id = ? OR subject_user_id = ? OR "
            "(scope = 'private' AND chat_id = ?) ORDER BY created_at", (uid, uid, uid))),
        "messages": _dicts(q("SELECT * FROM messages WHERE sender_user_id = ? OR chat_id = ? "
                             "ORDER BY chat_id, message_id", (uid, uid))),
        "relationships": _dicts(q("SELECT * FROM relationships WHERE user_id = ?", (uid,))),
    }


class BackupScheduler:
    """Компонент Lifecycle: бэкап раз в interval_h часов (первый — через минуту после старта)."""

    def __init__(self, db_path: Path, dest: Path, *, interval_h: float, keep: int,
                 retain_days: int, first_delay_s: float = 60.0) -> None:
        self._db_path, self._dest = db_path, dest
        self._interval = interval_h * 3600
        self._keep, self._retain = keep, retain_days
        self._first = first_delay_s
        self._task: asyncio.Task[None] | None = None
        self.last: Path | None = None

    async def run_once(self) -> Path | None:
        try:
            self.last = await asyncio.to_thread(create_backup, self._db_path, self._dest,
                                                keep=self._keep, retain_days=self._retain)
        except (BackupError, OSError, sqlite3.Error) as exc:
            log.error("бэкап не создан: %s", exc)
            return None
        log.info("бэкап: %s", self.last.name)
        return self.last

    async def _loop(self) -> None:
        await asyncio.sleep(self._first)
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="backup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
