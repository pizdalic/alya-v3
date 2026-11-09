-- Phase 7: инициативы (Аля пишет первой), напоминания, пауза проактивности, каналы.

-- Каждое инициативное сообщение: лимиты, cooldown и «не писать повторно без ответа».
CREATE TABLE initiatives (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users (user_id) ON DELETE CASCADE,  -- NULL для групп
    kind        TEXT NOT NULL CHECK (kind IN ('dm', 'group')),
    reason      TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 300),
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_initiatives_chat_time ON initiatives (chat_id, created_at);
CREATE INDEX idx_initiatives_time ON initiatives (created_at);

-- Напоминания, о которых собеседник попросил Алю в разговоре.
CREATE TABLE reminders (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
    due_at      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX idx_reminders_due ON reminders (due_at) WHERE done_at IS NULL;

-- /pause: человек попросил не писать ему первой.
CREATE TABLE proactive_pauses (
    user_id    INTEGER PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    paused_at  TEXT NOT NULL
);

-- Каналы, на которые подписана Аля: выключатель и время последней сводки.
CREATE TABLE channel_state (
    chat_id         INTEGER PRIMARY KEY REFERENCES chats (chat_id) ON DELETE CASCADE,
    muted           INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    last_digest_at  TEXT
);
