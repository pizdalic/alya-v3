# Эксплуатация

Установка на сервер, вход в Telegram, бэкапы, обновление и откат, режим бота, модерация и плагины. Все команды — из каталога проекта на сервере.

## Установка (Ubuntu, Docker)

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2
sudo usermod -aG docker "$USER"     # перелогиниться после этого
git clone https://github.com/pizdalic/alya-v3.git alya && cd alya
cp config.example.toml config.toml
cp .env.example .env               # секреты: ALYA_LLM__API_KEY, ALYA_TELEGRAM__API_HASH и др.
chmod 600 .env
mkdir -p runtime && sudo chown 10001:10001 runtime
docker compose build
docker compose run --rm alya check-config
```

В `config.toml` задать как минимум `[app] owner_user_id`, `[telegram] api_id`, адрес OpenRouter и модели в `[llm]` / `[llm.roles.*]`. Секреты — только в `.env`.

## Вход в Telegram (userbot)

Один раз, интерактивно: код из Telegram и пароль 2FA вводятся вручную и нигде не сохраняются.

```bash
docker compose run --rm -it alya login
docker compose up -d
docker compose exec alya alya healthcheck
```

Сессия лежит в `runtime/td_database` — это полный доступ к аккаунту @AlysiaNya. Не копировать её на чужие машины и не выкладывать (SECURITY T14).

## Бэкапы

Секция `[backup]`: по умолчанию бэкап раз в 24 часа в `runtime/backups/`, хранится не больше 7 копий и не дольше 7 дней. Каждая копия делается через SQLite backup API (консистентно на работающей БД), проверяется `PRAGMA integrity_check` и получает права 600.

```bash
docker compose run --rm alya backup        # сделать бэкап сейчас
docker compose run --rm alya backups       # список бэкапов
```

Забытое (`/forget`, `/forget_me`, `/forget_user`, `/forget_chat`) исчезает из старых бэкапов по сроку хранения, а при восстановлении удаляется повторно по журналу удалений (ADR-0010). Поэтому `retain_days` — это и срок, через который забытое уходит из всех копий.

Бэкапы содержат всю память и переписку. Хранить их на том же уровне защиты, что и `runtime/`; если копировать наружу — шифровать.

### Восстановление

```bash
docker compose stop alya
docker compose run --rm alya restore runtime/backups/alya-YYYYMMDD-HHMMSS.sqlite3 --yes
docker compose up -d
```

Что делает restore: проверяет целостность бэкапа, сохраняет текущую БД рядом как `alya.sqlite3.pre-restore-<время>`, подставляет бэкап и заново применяет все удаления из журнала текущей БД — забытое не воскресает. Копия `pre-restore` тоже удаляется через `retain_days`. Восстанавливать только при остановленной Але. Markdown-зеркало памяти пересобирается из БД.

Проверить процедуру стоит один раз заранее, на копии: сделать бэкап, восстановить, убедиться, что Аля помнит то, что должна.

### Выгрузка данных человека

По запросу собеседника (`/export` в личке отдаёт только записи памяти) полная выгрузка делается на сервере:

```bash
docker compose run --rm alya export-user 123456789 --out runtime/export-123456789.json
```

Файл получает права 600: передать человеку и удалить с сервера.

## Обновление и откат

```bash
docker compose run --rm alya backup        # всегда перед обновлением
git fetch && git checkout <новая версия>
docker compose build
docker compose run --rm alya check-config
docker compose up -d                       # миграции применяются при старте
docker compose exec alya alya healthcheck
```

Откат: миграции только вперёд, поэтому старый код на новой схеме не запускается — откатываются код и БД вместе.

```bash
docker compose stop alya
git checkout <прежняя версия> && docker compose build
docker compose run --rm alya restore runtime/backups/<бэкап до обновления> --yes
docker compose up -d
```

## Режим бота (запасной)

`[telegram] mode = "bot"` и `ALYA_TELEGRAM__BOT_TOKEN` в `.env` — Аля работает через Bot API вместо userbot-аккаунта (например, если аккаунт ограничен). Токен — такой же секрет, как сессия: он вырезается из логов и ошибок.

Ограничения Bot API:

- в группах бот видит все сообщения только при выключенном privacy mode (@BotFather → `/setprivacy` → Disable), иначе — только команды и ответы себе;
- нет голосовых и фото (получение файлов), вступления по ссылке, смены bio и аватара, отметок о прочтении, событий удаления сообщений;
- бот не может написать первым тому, кто не запускал его, — проактивность в личке работает только с теми, кто уже писал.

Текст, реакции, стикеры, ответы с цитатой и «печатает…» работают.

## Модерация

`[moderation] enabled = true` включает только dry_run: правила флуда, ссылок и запрещённых слов пишут в `audit_log`, что Аля сделала бы (`moderation_dry_run`), но никого не банят и ничего не удаляют. Владелец и `exempt_user_ids` не проверяются. Автоматических наказаний нет намеренно (SECURITY T11): сначала стоит посмотреть журнал и подстроить пороги.

## Плагины

Плагин грузится, только если он указан в `[plugins.grants]` с выданными разрешениями; пример — `examples/plugins/dice.py`. Плагины работают в том же процессе: разрешения защищают от ошибок, а не от вредоносного кода. Ставить только код, который прочитан и которому доверяешь (SECURITY T20).

## Мониторинг

- `docker compose exec alya alya healthcheck` — БД, Telegram, фоновые компоненты;
- `docker compose logs -f --tail 200 alya` — логи без текстов сообщений и секретов;
- бюджет: лимиты ответов (`replies_per_10min`), медиа (`[media.per_hour]`, `[media.per_day]`) и проактивности ограничивают число вызовов модели; лимит расходов в OpenRouter стоит задать и на стороне ключа.
