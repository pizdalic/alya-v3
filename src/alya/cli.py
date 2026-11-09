"""Командная строка: alya check-config | migrate | run | login | backup | backups | restore |
export-user | chat | persona | dream | healthcheck | version."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from alya import __version__
from alya.core.config import Settings, load_settings
from alya.core.errors import AlyaError, ConfigError

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_INVALID = 2
DEFAULT_CONFIG = Path("config.toml")


def _resolve_config_path(arg: str | None) -> Path | None:
    if arg:
        return Path(arg)
    env_path = os.environ.get("ALYA_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(_resolve_config_path(args.config))


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = _load(args)
    for key, value in settings.summary().items():
        print(f"{key}: {value}")
    problems = settings.problems_for_run()
    if problems:
        print("\nНе хватает для работы:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_PROBLEMS
    print("\nКонфигурация в порядке.")
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    from alya.storage.db import Database
    from alya.storage.migrator import apply_migrations

    settings = _load(args)
    db = Database(settings.storage.db_path, settings.storage.busy_timeout_ms)
    db.connect()
    try:
        applied = apply_migrations(db.conn)
    finally:
        db.close()
    print(f"применены: {applied}" if applied else "схема уже актуальна")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from alya.app import build_app
    from alya.observability.logs import setup_logging

    settings = _load(args)
    setup_logging(settings.logging.level, settings.logging.format, settings.secret_values())
    asyncio.run(build_app(settings).run())
    return EXIT_OK


def cmd_persona(args: argparse.Namespace) -> int:
    from alya.character.allocator import estimate_tokens
    from alya.character.persona import available_personas, load_character

    settings = _load(args)
    cs = settings.character
    character = load_character(cs.dir, cs.persona)
    prompt = character.core_prompt()
    print(f"характер: {character.persona.id} ({character.persona.title}); "
          f"доступны: {', '.join(available_personas(cs.dir))}")
    print(f"ядро промпта: {len(prompt)} символов, ~{estimate_tokens(prompt)} токенов\n")
    print(prompt)
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    """Интерактивный вход в аккаунт Telegram (код и пароль 2FA не сохраняются)."""
    settings = _load(args)
    if settings.telegram.mode != "userbot":
        print("вход нужен только для telegram.mode = userbot", file=sys.stderr)
        return EXIT_PROBLEMS
    return asyncio.run(_login(settings))


async def _login(settings: Settings) -> int:
    import getpass

    from alya.telegram.service import interactive_login

    async def prompt(kind: str, hint: str) -> str:
        reader = getpass.getpass if kind == "password" else input
        return await asyncio.to_thread(reader, f"{hint}: ")

    me = await interactive_login(settings, prompt)
    username = (me.get("usernames") or {}).get("active_usernames") or []
    shown = "@" + username[0] if username else str(me.get("first_name") or "")
    print(f"вход выполнен: {shown} (id {me.get('id')}); сессия сохранена, можно запускать alya run")
    return EXIT_OK


def cmd_chat(args: argparse.Namespace) -> int:
    """Поговорить с Алей в терминале (как владелец, в личке) — без Telegram."""
    settings = _load(args)
    return asyncio.run(_chat_loop(settings))


async def _chat_loop(settings: Settings) -> int:
    from alya.app import build_character, build_memory
    from alya.character.allocator import HistoryLine
    from alya.character.engine import Turn
    from alya.core.timeutil import iso, utcnow
    from alya.memory.models import MemoryContext
    from alya.storage.db import Database
    from alya.storage.entities import add_member, upsert_chat, upsert_user
    from alya.storage.migrator import apply_migrations

    db = Database(settings.storage.db_path, settings.storage.busy_timeout_ms)
    db.connect()
    try:
        apply_migrations(db.conn)
        memory, _ = build_memory(settings, db)
        engine = build_character(settings, db, memory)
        if engine is None:
            print("нужны llm.api_key и llm.roles.chat", file=sys.stderr)
            return EXIT_PROBLEMS
        uid = settings.app.owner_user_id or 1
        now = iso(utcnow())

        def seed(conn):  # type: ignore[no-untyped-def]
            upsert_user(conn, uid, now)
            upsert_chat(conn, uid, "private", now)
            add_member(conn, uid, uid, now)

        await db.transaction(seed)
        ctx = MemoryContext.private(uid)
        history: list[HistoryLine] = []
        print("чат с Алей; пустая строка или Ctrl+D — выход")
        while True:
            try:
                text = (await asyncio.to_thread(input, "ты> ")).strip()
            except EOFError:
                break
            if not text:
                break
            reply = await engine.respond(Turn(ctx, text, "ты", list(history)))
            history.append(HistoryLine("ты", text))
            for msg in reply.messages:
                print(f"аля> {msg}")
                history.append(HistoryLine("аля", msg, is_alya=True))
            if not reply.messages:
                print("аля молчит…")
            history = history[-40:]
    finally:
        db.close()
    return EXIT_OK


def cmd_dream(args: argparse.Namespace) -> int:
    """Один прогон консолидации памяти вручную (можно при работающей Але)."""
    return asyncio.run(_dream(_load(args)))


async def _dream(settings: Settings) -> int:
    from alya.app import build_dream, build_memory
    from alya.storage.db import Database
    from alya.storage.migrator import apply_migrations

    db = Database(settings.storage.db_path, settings.storage.busy_timeout_ms)
    db.connect()
    try:
        apply_migrations(db.conn)
        memory, _ = build_memory(settings, db)
        report = await build_dream(settings, db, memory).run("manual")
    finally:
        db.close()
    print(json.dumps(report.stats(), ensure_ascii=False))
    return EXIT_OK


def cmd_healthcheck(args: argparse.Namespace) -> int:
    url = args.url
    if url is None:
        settings = _load(args)
        url = f"http://127.0.0.1:{settings.health.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(body, ensure_ascii=False))
            return EXIT_OK if resp.status == 200 else EXIT_PROBLEMS
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"healthcheck недоступен: {exc}", file=sys.stderr)
    return EXIT_PROBLEMS


def cmd_backup(args: argparse.Namespace) -> int:
    from alya.storage.backup import create_backup
    settings = _load(args)
    bk = settings.backup
    path = create_backup(settings.storage.db_path, bk.dir, keep=bk.keep,
                         retain_days=bk.retain_days)
    print(f"бэкап: {path}")
    return EXIT_OK


def cmd_backups(args: argparse.Namespace) -> int:
    from alya.storage.backup import list_backups
    settings = _load(args)
    found = list_backups(settings.backup.dir)
    if not found:
        print("бэкапов нет")
    for p in found:
        print(f"{p}  {p.stat().st_size // 1024} КБ")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    from alya.storage.backup import list_backups, restore_backup
    settings = _load(args)
    found = list_backups(settings.backup.dir)
    if args.list:
        for p in found:
            print(p)
        return EXIT_OK
    src = Path(args.file) if args.file else (found[-1] if found else None)
    if src is None:
        print("бэкапов нет", file=sys.stderr)
        return EXIT_PROBLEMS
    if not args.yes:
        print(f"Будет восстановлено из {src}. Сначала остановите Алю, потом "
              f"повторите с --yes.", file=sys.stderr)
        return EXIT_PROBLEMS
    rep = restore_backup(src, settings.storage.db_path)
    print(f"восстановлено из {rep.restored_from}")
    if rep.safety_copy is not None:
        print(f"прежняя БД сохранена: {rep.safety_copy}")
    print(f"журнал удалений применён заново: {rep.tombstones} записей, "
          f"удалено строк: {rep.removed}")
    return EXIT_OK


def cmd_export_user(args: argparse.Namespace) -> int:
    import sqlite3

    from alya.storage.backup import export_user
    settings = _load(args)
    conn = sqlite3.connect(f"file:{settings.storage.db_path}?mode=ro", uri=True)
    try:
        data = export_user(conn, args.user_id)
    finally:
        conn.close()
    out = Path(args.out or f"export-{args.user_id}.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out.chmod(0o600)
    print(f"{out}: записей {len(data['memories'])}, сообщений {len(data['messages'])}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alya", description="Аля — Telegram-компаньон")
    parser.add_argument("--config", help="путь к config.toml (или ALYA_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="проверить конфигурацию").set_defaults(fn=cmd_check_config)
    sub.add_parser("migrate", help="применить миграции БД").set_defaults(fn=cmd_migrate)
    sub.add_parser("run", help="запустить").set_defaults(fn=cmd_run)
    sub.add_parser("login", help="войти в аккаунт Telegram").set_defaults(fn=cmd_login)
    sub.add_parser("chat", help="поговорить с Алей в терминале").set_defaults(fn=cmd_chat)
    sub.add_parser("persona", help="показать загруженный характер").set_defaults(fn=cmd_persona)
    sub.add_parser("dream", help="один прогон консолидации памяти").set_defaults(fn=cmd_dream)
    sub.add_parser("backup", help="создать резервную копию БД").set_defaults(fn=cmd_backup)
    sub.add_parser("backups", help="список бэкапов").set_defaults(fn=cmd_backups)
    rs = sub.add_parser("restore", help="восстановить БД из бэкапа (Аля остановлена)")
    rs.add_argument("file", nargs="?", help="файл бэкапа; по умолчанию — самый свежий")
    rs.add_argument("--list", action="store_true", help="показать бэкапы")
    rs.add_argument("--yes", action="store_true", help="подтвердить восстановление")
    rs.set_defaults(fn=cmd_restore)
    ex = sub.add_parser("export-user", help="выгрузить данные человека в JSON")
    ex.add_argument("user_id", type=int)
    ex.add_argument("-o", "--out", help="файл (по умолчанию export-<id>.json)")
    ex.set_defaults(fn=cmd_export_user)
    hc = sub.add_parser("healthcheck", help="опросить /health")
    hc.add_argument("--url")
    hc.add_argument("--timeout", type=float, default=5.0)
    hc.set_defaults(fn=cmd_healthcheck)
    sub.add_parser("version").set_defaults(fn=lambda a: print(__version__) or EXIT_OK)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.fn(args)
    except ConfigError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except AlyaError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    return code
