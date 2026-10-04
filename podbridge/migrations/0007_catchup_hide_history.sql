-- Hidden episodes, local "played" marks, catch-up batches (with undo) and a watch history.

ALTER TABLE episodes ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1));
-- Set by catch-up for episodes with no Pocket Casts match: played as far as PodBridge is concerned.
ALTER TABLE episodes ADD COLUMN marked_played_at TEXT;

CREATE TABLE catch_up_batches (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    label        TEXT NOT NULL,
    scope        TEXT NOT NULL,       -- the show it was run from: '<source id>/<podcast uuid or ->'
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed', 'undoing', 'undone')),
    total        INTEGER NOT NULL DEFAULT 0,
    processed    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

-- What each episode looked like before the batch, so Undo can put it back.
CREATE TABLE catch_up_items (
    batch_id               INTEGER NOT NULL REFERENCES catch_up_batches (id) ON DELETE CASCADE,
    episode_id             INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    prev_pc_status         INTEGER,
    prev_pc_position       REAL,
    prev_marked_played_at  TEXT,
    done                   INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    PRIMARY KEY (batch_id, episode_id)
);

-- Every observed change in progress, on either side.
CREATE TABLE watch_history (
    id            INTEGER PRIMARY KEY,
    episode_id    INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    side          TEXT NOT NULL CHECK (side IN ('patreon', 'youtube', 'pocketcasts')),
    position_secs REAL,
    played        INTEGER NOT NULL DEFAULT 0 CHECK (played IN (0, 1)),
    at            TEXT NOT NULL
);
CREATE INDEX watch_history_at ON watch_history (at);
CREATE INDEX watch_history_episode ON watch_history (episode_id, at);

-- Backfill history from what's already known: the latest source position (Patreon gives a real
-- timestamp) and the latest Pocket Casts change PodBridge noticed.
INSERT INTO watch_history (episode_id, side, position_secs, played, at)
SELECT p.episode_id, CASE s.kind WHEN 'youtube' THEN 'youtube' ELSE 'patreon' END,
       p.patreon_position_secs,
       -- finished by the sync rule (within 60 s of the end), not Patreon's early "watched" flag
       CASE WHEN e.duration_secs IS NOT NULL AND p.patreon_position_secs >= e.duration_secs - 60 THEN 1
            WHEN p.patreon_is_watched = 1 AND (p.patreon_position_secs IS NULL OR e.duration_secs IS NULL) THEN 1
            ELSE 0 END,
       strftime('%Y-%m-%dT%H:%M:%SZ', p.patreon_updated_at)
FROM progress p JOIN episodes e ON e.id = p.episode_id JOIN sources s ON s.id = e.source_id
WHERE p.patreon_updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%SZ', p.patreon_updated_at) IS NOT NULL;

INSERT INTO watch_history (episode_id, side, position_secs, played, at)
SELECT episode_id, 'pocketcasts', pocketcasts_position_secs, CASE pocketcasts_status WHEN 3 THEN 1 ELSE 0 END,
       pocketcasts_changed_at
FROM progress WHERE pocketcasts_changed_at IS NOT NULL;
