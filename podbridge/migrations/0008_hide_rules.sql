-- Per-source auto-hide rules.
CREATE TABLE hide_rules (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('title_contains', 'shorter_than', 'unmatched_after_days')),
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Which rule hid an episode (NULL = hidden by hand, or not hidden).
ALTER TABLE episodes ADD COLUMN hidden_by_rule INTEGER;
-- Set when you unhide an episode a rule had hidden: rules leave it alone from then on.
ALTER TABLE episodes ADD COLUMN hide_override INTEGER NOT NULL DEFAULT 0 CHECK (hide_override IN (0, 1));
