"""Командная строка: alya check-config | migrate | run | healthcheck | version."""

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alya", description="Аля — Telegram-компаньон")
    parser.add_argument("--config", help="путь к config.toml (или ALYA_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="проверить конфигурацию").set_defaults(fn=cmd_check_config)
    sub.add_parser("migrate", help="применить миграции БД").set_defaults(fn=cmd_migrate)
    sub.add_parser("run", help="запустить").set_defaults(fn=cmd_run)
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
