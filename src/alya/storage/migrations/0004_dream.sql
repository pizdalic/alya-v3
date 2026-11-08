-- Phase 5: журнал прогонов Dream Engine и проверенные пары записей.
-- Каждое изменение памяти Dream делает в своей транзакции; прогон, прерванный
-- на середине, остаётся со статусом running и при следующем старте помечается interrupted.

CREATE TABLE dream_runs (
    id          INTEGER PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT 'schedule' CHECK (length(reason) BETWEEN 1 AND 32),
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed', 'interrupted')),
    stats       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stats)),
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_dream_runs_started ON dream_runs (started_at);

-- Пары, которые LLM уже сравнила: не платим за одно и то же дважды.
-- Удаление любой записи (Forget) удаляет и пару.
CREATE TABLE dream_checked_pairs (
    a_id       TEXT NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    b_id       TEXT NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL CHECK (verdict IN ('same', 'contradicts', 'unrelated')),
    checked_at TEXT NOT NULL,
    PRIMARY KEY (a_id, b_id),
    CHECK (a_id < b_id)
);
CREATE INDEX idx_dream_pairs_b ON dream_checked_pairs (b_id);
