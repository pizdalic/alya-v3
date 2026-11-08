-- Базовая схема: люди, чаты, сообщения, outbox, аудит, журнал удалений.
-- Время везде — ISO-8601 в UTC. Таблицы памяти появятся в Phase 2.

CREATE TABLE users (
    user_id       INTEGER PRIMARY KEY,                -- Telegram user id
    username      TEXT,
    display_name  TEXT,
    is_owner      INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
    is_blocked    INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1)),
    timezone      TEXT,                               -- NULL = таймзона приложения
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE chats (
    chat_id    INTEGER PRIMARY KEY,                   -- Telegram chat id
    kind       TEXT NOT NULL CHECK (kind IN ('private', 'group', 'supergroup', 'channel')),
    title      TEXT,
    policy     TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE chat_members (
    chat_id    INTEGER NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE messages (
    id                     INTEGER PRIMARY KEY,
    chat_id                INTEGER NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    tg_message_id          INTEGER NOT NULL,
    sender_user_id         INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
    is_outgoing            INTEGER NOT NULL DEFAULT 0 CHECK (is_outgoing IN (0, 1)),
    reply_to_tg_message_id INTEGER,
    addressed_to_alya      INTEGER NOT NULL DEFAULT 0 CHECK (addressed_to_alya IN (0, 1)),
    context_only           INTEGER NOT NULL DEFAULT 0 CHECK (context_only IN (0, 1)),
    text                   TEXT,
    media_ref              TEXT,
    created_at             TEXT NOT NULL,
    edited_at              TEXT,
    expires_at             TEXT,                      -- TTL; NULL = без срока
    UNIQUE (chat_id, tg_message_id)
);
CREATE INDEX idx_messages_chat_time ON messages (chat_id, created_at);
CREATE INDEX idx_messages_expires ON messages (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY,
    chat_id         INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload         TEXT NOT NULL CHECK (json_valid(payload)),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_outbox_due ON outbox (status, next_attempt_at);

CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY,
    actor_user_id INTEGER,
    action        TEXT NOT NULL,
    target        TEXT,
    details       TEXT CHECK (details IS NULL OR json_valid(details)),
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_audit_time ON audit_log (created_at);

-- Журнал удалений (ADR-0010): только идентификаторы, без содержимого.
CREATE TABLE deletion_journal (
    id           INTEGER PRIMARY KEY,
    target_type  TEXT NOT NULL CHECK (target_type IN ('user', 'chat', 'message', 'memory')),
    target_id    TEXT NOT NULL,
    requested_by INTEGER,
    created_at   TEXT NOT NULL
);
