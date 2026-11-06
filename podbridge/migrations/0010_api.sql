-- The outside API (Design Uniformity API-STANDARD.md): tokens, the action log, idempotency keys,
-- events (what /api/v1/events streams and webhooks send) and webhooks. Times are UTC ISO text,
-- like the rest of PodBridge.

CREATE TABLE api_tokens (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,      -- sha256 of the token; the token itself is shown once
    scope         TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);

CREATE TABLE api_audit (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    token_id    INTEGER,                      -- NULL: the admin session
    token_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      INTEGER NOT NULL,
    message     TEXT
);
CREATE INDEX api_audit_at ON api_audit (at);

CREATE TABLE api_idempotency (
    token_id    INTEGER NOT NULL,             -- 0: the admin session
    key         TEXT NOT NULL,
    action      TEXT NOT NULL,
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (token_id, key)
);

CREATE TABLE events (
    id     INTEGER PRIMARY KEY,               -- the SSE id and Last-Event-ID
    type   TEXT NOT NULL,                     -- dotted, e.g. sync.finished
    at     TEXT NOT NULL,
    title  TEXT NOT NULL,
    item   TEXT NOT NULL                      -- JSON, the common item shape
);
CREATE INDEX events_at ON events (at);

CREATE TABLE webhooks (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    url            TEXT NOT NULL,             -- a secret too (a Home Assistant webhook id is the credential)
    secret         TEXT NOT NULL,
    event_types    TEXT NOT NULL DEFAULT '[]',  -- JSON list; [] = every type
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    last_ok_at     TEXT,
    last_error     TEXT,
    last_error_at  TEXT
);

CREATE TABLE webhook_deliveries (
    id          INTEGER PRIMARY KEY,
    webhook_id  INTEGER NOT NULL REFERENCES webhooks (id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT NOT NULL,
    done_at     TEXT,
    error       TEXT
);
CREATE INDEX webhook_deliveries_due ON webhook_deliveries (done_at, next_at);
