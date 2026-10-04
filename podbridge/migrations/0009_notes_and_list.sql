-- Show notes, fetched when an episode page is first opened and cached here.
CREATE TABLE episode_notes (
    episode_id   INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
    source_text  TEXT,      -- Patreon post text or YouTube description, as plain text
    pc_text      TEXT,      -- Pocket Casts show notes, as plain text
    fetched_at   TEXT NOT NULL
);

-- My List: episodes saved for later, in your order.
CREATE TABLE my_list (
    episode_id  INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Pocket Casts stars: the latest seen, and the last one mirrored into My List. When they
-- differ, the star changed in Pocket Casts and My List follows it.
ALTER TABLE pocketcasts_episodes ADD COLUMN starred INTEGER CHECK (starred IN (0, 1));
ALTER TABLE pocketcasts_episodes ADD COLUMN starred_applied INTEGER CHECK (starred_applied IN (0, 1));
