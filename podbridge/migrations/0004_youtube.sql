-- YouTube channels as sources.
--
-- Naming note: the episode/progress columns prefixed `patreon_` hold the
-- source-side values for every source kind. For YouTube sources:
--   sources.campaign_id      = channel ID (UC...), collection_id = ''
--   episodes.patreon_post_id = video ID, patreon_url = watch URL
--   progress.patreon_*       = position derived from the history "percent watched" bar

ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'patreon' CHECK (kind IN ('patreon', 'youtube'));

-- Watch-page details, fetched once per video (any channel, so non-matching videos aren't refetched).
CREATE TABLE youtube_videos (
    video_id       TEXT PRIMARY KEY,
    channel_id     TEXT,
    title          TEXT,
    published_at   TEXT,
    duration_secs  REAL,
    fetched_at     TEXT NOT NULL
);

-- Rebuild episodes to allow the 'auto_date' match method (date + title similarity, for YouTube).
CREATE TABLE episodes_new (
    id                       INTEGER PRIMARY KEY,
    source_id                INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    patreon_post_id          TEXT NOT NULL,
    patreon_media_id         TEXT,
    patreon_url              TEXT,
    title                    TEXT NOT NULL,
    published_at             TEXT,
    duration_secs            REAL,
    pocketcasts_episode_uuid TEXT,
    match_method             TEXT NOT NULL DEFAULT 'none'
                             CHECK (match_method IN ('auto_title', 'auto_duration', 'auto_date', 'manual', 'none')),
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    match_locked             INTEGER NOT NULL DEFAULT 0 CHECK (match_locked IN (0, 1)),
    UNIQUE (source_id, patreon_post_id)
);
INSERT INTO episodes_new (id, source_id, patreon_post_id, patreon_media_id, patreon_url, title, published_at,
                          duration_secs, pocketcasts_episode_uuid, match_method, created_at, updated_at, match_locked)
SELECT id, source_id, patreon_post_id, patreon_media_id, patreon_url, title, published_at,
       duration_secs, pocketcasts_episode_uuid, match_method, created_at, updated_at, match_locked
FROM episodes;
DROP TABLE episodes;
ALTER TABLE episodes_new RENAME TO episodes;
CREATE INDEX episodes_pocketcasts_uuid ON episodes (pocketcasts_episode_uuid);
