"""Patreon session health check and episode discovery into the database.

One list sweep per source returns every post with its media ID and progress
(Phase 0 finding), so discovery also refreshes the Patreon side of `progress`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .db import utcnow
from .patreon import PatreonClient, PatreonSessionExpired, Post
from .settings_store import SettingsStore

log = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    source_id: int
    label: str
    posts_seen: int = 0
    added: int = 0
    refreshed: int = 0
    skipped_no_media: int = 0


def check_patreon_session(store: SettingsStore, client: PatreonClient) -> bool:
    """Run the health check and record the outcome for the UI and /healthz."""
    ok = client.check_session()
    store.set("patreon_status", "ok" if ok else "expired")
    store.set("patreon_verified_at", utcnow())
    return ok


def upsert_post(conn: sqlite3.Connection, source_id: int, post: Post) -> bool:
    """Insert or refresh one episode and its Patreon progress. Returns True if newly added."""
    existing = conn.execute(
        "SELECT id FROM episodes WHERE source_id = ? AND patreon_post_id = ?", (source_id, post.post_id)
    ).fetchone()
    now = utcnow()
    if existing:
        episode_id = existing["id"]
        conn.execute(
            "UPDATE episodes SET patreon_media_id = ?, patreon_url = ?, title = ?, published_at = ?, "
            "duration_secs = ?, updated_at = ? WHERE id = ?",
            (post.media_id, post.url, post.title, post.published_at, post.duration_secs, now, episode_id),
        )
    else:
        episode_id = conn.execute(
            "INSERT INTO episodes (source_id, patreon_post_id, patreon_media_id, patreon_url, title, "
            "published_at, duration_secs) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, post.post_id, post.media_id, post.url, post.title, post.published_at,
             post.duration_secs),
        ).lastrowid
    p = post.progress
    conn.execute(
        "INSERT INTO progress (episode_id, patreon_position_secs, patreon_is_watched, patreon_watch_state, "
        "patreon_updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (episode_id) DO UPDATE SET patreon_position_secs = excluded.patreon_position_secs, "
        "patreon_is_watched = excluded.patreon_is_watched, patreon_watch_state = excluded.patreon_watch_state, "
        "patreon_updated_at = excluded.patreon_updated_at",
        (episode_id, p.position_secs, int(p.is_watched), p.watch_state, p.updated_at),
    )
    return existing is None


def discover_source(conn: sqlite3.Connection, client: PatreonClient, source: sqlite3.Row) -> DiscoveryResult:
    result = DiscoveryResult(source_id=source["id"], label=source["label"])
    posts = client.list_posts(source["campaign_id"], source["collection_id"])
    with conn:
        for post in posts:
            result.posts_seen += 1
            if not post.media_id:
                result.skipped_no_media += 1
                continue
            if upsert_post(conn, source["id"], post):
                result.added += 1
            else:
                result.refreshed += 1
    log.info("Discovery for source %s: %s", source["id"], result)
    return result


def discover_all(conn: sqlite3.Connection, store: SettingsStore, client: PatreonClient) -> list[DiscoveryResult]:
    """Health check first: a dead session returns logged-out defaults that look like real data."""
    if not check_patreon_session(store, client):
        raise PatreonSessionExpired("Patreon session is not logged in")
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY id").fetchall()
    return [discover_source(conn, client, source) for source in sources]
