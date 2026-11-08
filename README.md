# Аля

Telegram-компаньон, живущий в собственном аккаунте (userbot): помнит людей и разговоры, понимает, когда в группе обращаются к ней, и пишет по-русски. Проект пишется с нуля.

## Статус

**Phase 2 — Memory Engine.** Готовы основа (конфигурация, DI, Event Bus, SQLite с миграциями, жизненный цикл, логирование без секретов, healthcheck, Docker, CI) и память: изоляция по области видимости (личка / чат / персонаж), уровни working / short / long с TTL, поиск FTS5 + эмбеддинги + RRF (без эмбеддингов работает только FTS5), Forget API с журналом удалений, Markdown-зеркало только для чтения. Telegram и персонаж появятся в следующих фазах — см. [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Быстрый старт (Ubuntu, Docker)

```bash
cp config.example.toml config.toml
cp .env.example .env          # заполнить секреты
mkdir -p runtime && sudo chown 10001:10001 runtime
docker compose build
docker compose run --rm alya check-config
docker compose up -d
docker compose exec alya alya healthcheck
```

## Локальная разработка

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
alya check-config
alya migrate
pytest -q && ruff check . && mypy
```

Команды: `alya check-config`, `alya migrate`, `alya run`, `alya healthcheck`, `alya version`.

Конфигурация: `config.toml` + переменные окружения `ALYA_<СЕКЦИЯ>__<КЛЮЧ>` (окружение важнее файла). Секреты (`api_hash`, `api_key`, `bot_token`) принимаются только из окружения.

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — архитектура
- [docs/DECISIONS.md](docs/DECISIONS.md) — принятые решения
- [docs/ADR/](docs/ADR/) — архитектурные решения
- [docs/SECURITY.md](docs/SECURITY.md) — модель угроз
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) — зависимости
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) — план

## Лицензия

[PolyForm Noncommercial 1.0.0](LICENSE): можно использовать, копировать и изменять в некоммерческих целях; продавать и использовать коммерчески нельзя. См. [ADR-0006](docs/ADR/0006-license.md).
