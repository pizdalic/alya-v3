"""Миграции: нумерованные SQL-файлы NNNN_name.sql, checksum, только вперёд.

Каждая миграция применяется атомарно вместе с записью в schema_migrations.
Изменение уже применённой миграции (другая checksum) — ошибка, а не тихий пропуск.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable

from alya.core.errors import MigrationError

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_TX_RE = re.compile(
    r"^\s*(BEGIN(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?|COMMIT|ROLLBACK|END\s+TRANSACTION)\s*;",
    re.IGNORECASE | re.MULTILINE,
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def default_directory() -> Traversable:
    return resources.files("alya.storage").joinpath("migrations")


def load_migrations(directory: Traversable | None = None) -> list[Migration]:
    root = directory if directory is not None else default_directory()
    found: dict[int, Migration] = {}
    for entry in root.iterdir():
        match = _FILE_RE.match(entry.name)
        if not entry.is_file() or match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"две миграции с номером {version:04d}")
        sql = entry.read_text(encoding="utf-8")
        if _TX_RE.search(sql):
            raise MigrationError(f"{entry.name}: управление транзакцией внутри миграции запрещено")
        found[version] = Migration(version=version, name=match.group(2), sql=sql)
    migrations = [found[v] for v in sorted(found)]
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise MigrationError("номера миграций должны идти подряд с 0001")
    return migrations


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    conn.execute(_CREATE_TABLE)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def pending(conn: sqlite3.Connection, migrations: list[Migration]) -> list[Migration]:
    applied = applied_versions(conn)
    known = {m.version for m in migrations}
    newer = sorted(v for v in applied if v not in known)
    if newer:
        raise MigrationError(f"БД новее кода: неизвестные миграции {newer}")
    result = []
    for m in migrations:
        if m.version in applied:
            if applied[m.version] != m.checksum:
                raise MigrationError(f"миграция {m.version:04d}_{m.name} изменена после применения")
            continue
        result.append(m)
    return result


def apply_migrations(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Применить неприменённые миграции. Идемпотентно. Возвращает номера применённых."""
    items = load_migrations() if migrations is None else migrations
    done: list[int] = []
    for m in pending(conn, items):
        now = datetime.now(UTC).isoformat()
        # executescript сам коммитит открытую транзакцию, поэтому BEGIN/COMMIT — внутри скрипта.
        # Подставляемые значения безопасны: int, [a-z0-9_], hex, ISO-дата.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{m.sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            f"VALUES ({m.version}, '{m.name}', '{m.checksum}', '{now}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"миграция {m.version:04d}_{m.name} не применена: {exc}") from exc
        done.append(m.version)
    return done
