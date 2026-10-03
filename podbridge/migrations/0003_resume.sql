-- When PodBridge first saw the Pocket Casts position or status change (the API
-- gives no timestamp of its own). Used to order "Continue watching".
ALTER TABLE progress ADD COLUMN pocketcasts_changed_at TEXT;
