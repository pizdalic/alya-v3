-- Phase 3: состояние персонажа, эмоциональный дневник, отношения, бюджет стиля.

-- Глобальное настроение (одно на всего персонажа, VAD в [-1, 1]).
CREATE TABLE character_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    persona     TEXT NOT NULL,
    valence     REAL NOT NULL CHECK (valence BETWEEN -1 AND 1),
    arousal     REAL NOT NULL CHECK (arousal BETWEEN -1 AND 1),
    dominance   REAL NOT NULL CHECK (dominance BETWEEN -1 AND 1),
    updated_at  TEXT NOT NULL
);

-- Эмоциональный дневник: что и почему сдвинуло настроение.
-- Привязан к чату и пользователю каскадом — Forget удаляет записи вместе с ними.
CREATE TABLE emotion_diary (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER REFERENCES chats (chat_id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users (user_id) ON DELETE CASCADE,
    reason      TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 300),
    d_valence   REAL NOT NULL,
    d_arousal   REAL NOT NULL,
    d_dominance REAL NOT NULL,
    valence     REAL NOT NULL,
    arousal     REAL NOT NULL,
    dominance   REAL NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_diary_created ON emotion_diary (created_at);
CREATE INDEX idx_diary_user ON emotion_diary (user_id);
CREATE INDEX idx_diary_chat ON emotion_diary (chat_id);

-- Отношение к человеку в конкретном чате.
CREATE TABLE relationships (
    user_id             INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id             INTEGER NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    affinity            REAL NOT NULL DEFAULT 0 CHECK (affinity BETWEEN -1 AND 1),
    trust               REAL NOT NULL DEFAULT 0.3 CHECK (trust BETWEEN 0 AND 1),
    interactions        INTEGER NOT NULL DEFAULT 0 CHECK (interactions >= 0),
    last_interaction_at TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);
CREATE INDEX idx_relationships_chat ON relationships (chat_id);

-- Скользящее окно последних исходящих сообщений для бюджета опечаток/эмодзи.
CREATE TABLE style_window (
    chat_id    INTEGER PRIMARY KEY REFERENCES chats (chat_id) ON DELETE CASCADE,
    recent     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(recent)),
    updated_at TEXT NOT NULL
);
