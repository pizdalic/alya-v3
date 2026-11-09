-- Phase 6: стикеры из разрешённых наборов и учёт расхода медиа-функций.

-- Стикеры, которые Але разрешено отправлять (наборы добавляет владелец командой /stickers).
CREATE TABLE stickers (
    remote_id   TEXT PRIMARY KEY CHECK (length(remote_id) BETWEEN 1 AND 256),
    set_name    TEXT NOT NULL CHECK (length(set_name) BETWEEN 1 AND 64),
    emoji       TEXT NOT NULL DEFAULT '' CHECK (length(emoji) <= 16),
    added_at    TEXT NOT NULL
);
CREATE INDEX idx_stickers_emoji ON stickers (emoji);
CREATE INDEX idx_stickers_set ON stickers (set_name);

-- Журнал платных и «публичных» действий: лимиты переживают перезапуск.
CREATE TABLE media_usage (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN
                    ('vision', 'stt', 'tts', 'image', 'search', 'reaction', 'sticker')),
    chat_id     INTEGER,
    at          TEXT NOT NULL
);
CREATE INDEX idx_media_usage_kind_at ON media_usage (kind, at);
