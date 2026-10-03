-- Cached artwork (series art, episode thumbnails). Files live in <data dir>/artwork;
-- source URLs are never stored (Patreon's are signed).
CREATE TABLE artwork (
    key           TEXT PRIMARY KEY,   -- e.g. 'yt:<video id>', 'patreon:<post id>', 'podcast:<uuid>'
    filename      TEXT,
    content_type  TEXT,
    ok            INTEGER NOT NULL DEFAULT 0 CHECK (ok IN (0, 1)),
    fetched_at    TEXT NOT NULL
);
