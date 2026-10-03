-- A source can feed several Pocket Casts podcasts (one YouTube channel can carry two shows,
-- e.g. The News Agents and The News Agents USA). sources.pocketcasts_podcast_uuid stays as
-- the first linked podcast, so "is this source linked?" checks keep working.
CREATE TABLE source_podcasts (
    source_id     INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    podcast_uuid  TEXT NOT NULL,
    title         TEXT,
    PRIMARY KEY (source_id, podcast_uuid)
);
INSERT INTO source_podcasts (source_id, podcast_uuid)
SELECT id, pocketcasts_podcast_uuid FROM sources WHERE pocketcasts_podcast_uuid IS NOT NULL;
