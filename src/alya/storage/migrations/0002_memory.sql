-- Память: три уровня (working / short / long), три области видимости (private / chat / character).
-- Правило видимости (ARCHITECTURE §5):
--   private   — только в личке владельца (owner_user_id + этот же chat_id);
--   chat      — только в этом групповом чате;
--   character — факты о самой Але, без данных пользователей.
-- subject_user_id — о ком запись: /forget пользователя удаляет и групповые записи о нём.

CREATE TABLE memories (
    seq             INTEGER PRIMARY KEY,              -- rowid для FTS5 (external content)
    id              TEXT NOT NULL UNIQUE,             -- ULID
    scope           TEXT NOT NULL CHECK (scope IN ('private', 'chat', 'character')),
    owner_user_id   INTEGER REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id         INTEGER REFERENCES chats (chat_id) ON DELETE CASCADE,
    subject_user_id INTEGER REFERENCES users (user_id) ON DELETE CASCADE,
    layer           TEXT NOT NULL CHECK (layer IN ('working', 'short', 'long')),
    category        TEXT NOT NULL DEFAULT 'fact',
    content         TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 4000),
    -- Текст для FTS: unicode61 не сводит «ё» к «е», делаем это сами.
    search_text     TEXT GENERATED ALWAYS AS (replace(replace(content, 'ё', 'е'), 'Ё', 'Е')) VIRTUAL,
    provenance      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provenance)),
    confidence      REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
    is_permanent    INTEGER NOT NULL DEFAULT 0 CHECK (is_permanent IN (0, 1)),
    usage_count     INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
    last_used_at    TEXT,
    superseded_by   TEXT REFERENCES memories (id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    expires_at      TEXT,
    CHECK (
        (scope = 'private' AND owner_user_id IS NOT NULL AND chat_id IS NOT NULL)
        OR (scope = 'chat' AND owner_user_id IS NULL AND chat_id IS NOT NULL)
        OR (scope = 'character' AND owner_user_id IS NULL AND chat_id IS NULL
            AND subject_user_id IS NULL)
    )
);
CREATE INDEX idx_memories_scope ON memories (scope, chat_id, owner_user_id);
CREATE INDEX idx_memories_subject ON memories (subject_user_id) WHERE subject_user_id IS NOT NULL;
CREATE INDEX idx_memories_expires ON memories (expires_at) WHERE expires_at IS NOT NULL;

CREATE VIRTUAL TABLE memories_fts USING fts5 (
    search_text,
    content = 'memories',
    content_rowid = 'seq',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, search_text) VALUES (new.seq, new.search_text);
END;

CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, search_text) VALUES ('delete', old.seq, old.search_text);
END;

CREATE TRIGGER memories_fts_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, search_text) VALUES ('delete', old.seq, old.search_text);
    INSERT INTO memories_fts (rowid, search_text) VALUES (new.seq, new.search_text);
END;

CREATE TABLE memory_embeddings (
    memory_id  TEXT NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    model_id   TEXT NOT NULL,
    dim        INTEGER NOT NULL CHECK (dim > 0),
    vector     BLOB NOT NULL CHECK (length(vector) = dim * 4),   -- float32 little-endian
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, model_id)
);
CREATE INDEX idx_embeddings_model ON memory_embeddings (model_id, dim);

CREATE TABLE memory_links (
    from_id    TEXT NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    relation   TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'derived_from', 'about')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, relation),
    CHECK (from_id <> to_id)
);
CREATE INDEX idx_links_to ON memory_links (to_id);
