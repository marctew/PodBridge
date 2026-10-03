-- Initial schema (spec section 6). Timestamps are ISO 8601 UTC strings.

CREATE TABLE sources (
    id                       INTEGER PRIMARY KEY,
    label                    TEXT NOT NULL,
    campaign_id              TEXT NOT NULL,
    collection_id            TEXT NOT NULL,
    pocketcasts_podcast_uuid TEXT,
    enabled                  INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (campaign_id, collection_id)
);

CREATE TABLE episodes (
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
                             CHECK (match_method IN ('auto_title', 'auto_duration', 'manual', 'none')),
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, patreon_post_id)
);
CREATE INDEX episodes_pocketcasts_uuid ON episodes (pocketcasts_episode_uuid);

CREATE TABLE progress (
    episode_id                     INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
    patreon_position_secs          REAL,
    patreon_is_watched             INTEGER CHECK (patreon_is_watched IN (0, 1)),
    patreon_watch_state            TEXT,
    patreon_updated_at             TEXT,
    pocketcasts_position_secs      REAL,
    pocketcasts_status             INTEGER CHECK (pocketcasts_status IN (1, 2, 3)),
    -- patreon_updated_at value at the last sync, for the "unchanged, skip" rule (spec 7, rule 2)
    synced_patreon_updated_at      TEXT,
    last_synced_at                 TEXT
);

CREATE TABLE sync_runs (
    id                INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    tier              TEXT NOT NULL CHECK (tier IN ('fast', 'full', 'manual')),
    dry_run           INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    episodes_checked  INTEGER NOT NULL DEFAULT 0,
    episodes_updated  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error', 'aborted')),
    error             TEXT
);
CREATE INDEX sync_runs_started ON sync_runs (started_at);

CREATE TABLE sync_events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES sync_runs (id) ON DELETE CASCADE,
    episode_id  INTEGER REFERENCES episodes (id) ON DELETE SET NULL,
    action      TEXT NOT NULL
                CHECK (action IN ('set_position', 'mark_played', 'skipped_behind', 'skipped_unchanged',
                                  'skipped_played', 'no_match')),
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX sync_events_run ON sync_events (run_id);

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Seed data only; nothing in code depends on this show.
INSERT INTO sources (label, campaign_id, collection_id)
VALUES ('Button Boys: Hidden Cache', '14434926', '1909234');
