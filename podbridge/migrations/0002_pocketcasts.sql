-- Pocket Casts catalogue cache (for matching and the manual match picker)
-- and a lock so a deliberate "no match" isn't undone by auto-matching.

CREATE TABLE pocketcasts_episodes (
    uuid            TEXT PRIMARY KEY,
    podcast_uuid    TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    duration_secs   REAL,
    playing_status  INTEGER CHECK (playing_status IN (1, 2, 3)),
    played_up_to    REAL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX pocketcasts_episodes_podcast ON pocketcasts_episodes (podcast_uuid);

ALTER TABLE episodes ADD COLUMN match_locked INTEGER NOT NULL DEFAULT 0 CHECK (match_locked IN (0, 1));
