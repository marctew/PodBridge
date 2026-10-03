"""Patreon session health check and episode discovery into the database.

One list sweep per source returns every post with its media ID and progress
(Phase 0 finding), so discovery also refreshes the Patreon side of `progress`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import utcnow
from .patreon import PatreonClient, PatreonSessionExpired, Post, Progress
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
    posts = client.list_posts(source["campaign_id"], source["collection_id"] or None)
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
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 AND kind = 'patreon' ORDER BY id").fetchall()
    return [discover_source(conn, client, source) for source in sources]


# --- YouTube ---

def has_sources(conn: sqlite3.Connection, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM sources WHERE enabled = 1 AND kind = ?", (kind,)).fetchone() is not None


def check_youtube_session(store: SettingsStore, client) -> bool:
    ok = client.check_session()
    store.set("youtube_status", "ok" if ok else "expired")
    store.set("youtube_verified_at", utcnow())
    return ok


DETAILS_RETRY_AFTER = timedelta(days=1)


def _cached_details(conn: sqlite3.Connection, client, video_id: str):
    """Watch-page details, fetched once. A failed lookup is cached too, and retried after a day."""
    from .youtube import YouTubeBlocked, YouTubeError, YouTubeSessionExpired

    row = conn.execute("SELECT * FROM youtube_videos WHERE video_id = ?", (video_id,)).fetchone()
    if row is not None:
        fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if row["channel_id"] or datetime.now(timezone.utc) - fetched < DETAILS_RETRY_AFTER:
            return row
    try:
        details = client.video_details(video_id)
    except (YouTubeBlocked, YouTubeSessionExpired):
        raise
    except YouTubeError:
        details = None  # removed/private video, or a page we can't parse
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO youtube_videos (video_id, channel_id, title, published_at, duration_secs, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, details.channel_id if details else None, details.title if details else None,
             details.published_at if details else None, details.duration_secs if details else None, utcnow()),
        )
    return conn.execute("SELECT * FROM youtube_videos WHERE video_id = ?", (video_id,)).fetchone()


def discover_youtube_all(conn: sqlite3.Connection, store: SettingsStore, client) -> list[DiscoveryResult]:
    """Recent watch history -> episodes for each enabled YouTube channel source.

    Position = percent watched x length (about 1% resolution). YouTube gives no
    'last watched' time, so the progress timestamp is 'now' whenever the percent changes.
    """
    from .youtube import YouTubeSessionExpired, watch_url

    if not check_youtube_session(store, client):
        raise YouTubeSessionExpired("YouTube cookies are no longer signed in")
    sources = {s["campaign_id"]: s for s in conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND kind = 'youtube' ORDER BY id")}
    results = {cid: DiscoveryResult(source_id=s["id"], label=s["label"]) for cid, s in sources.items()}
    for item in client.history():
        if item.percent is None:
            continue  # no progress bar: nothing to sync, so don't spend a request on it
        details = _cached_details(conn, client, item.video_id)
        source = sources.get(details["channel_id"])
        if source is None:
            continue
        result = results[source["campaign_id"]]
        result.posts_seen += 1
        duration = details["duration_secs"]
        position = round(item.percent / 100 * duration, 1) if duration else None
        existing = conn.execute(
            "SELECT p.patreon_position_secs, p.patreon_updated_at FROM episodes e "
            "JOIN progress p ON p.episode_id = e.id WHERE e.source_id = ? AND e.patreon_post_id = ?",
            (source["id"], item.video_id)).fetchone()
        unchanged = existing is not None and existing["patreon_position_secs"] == position
        updated_at = existing["patreon_updated_at"] if unchanged else utcnow()
        post = Post(
            post_id=item.video_id, title=details["title"] or item.title, published_at=details["published_at"],
            url=watch_url(item.video_id), post_type="youtube", media_id=item.video_id, duration_secs=duration,
            progress=Progress(position_secs=position, is_watched=False,
                              watch_state="is_watching" if position else "is_not_watched",
                              updated_at=updated_at if position else None),
        )
        with conn:
            if upsert_post(conn, source["id"], post):
                result.added += 1
            else:
                result.refreshed += 1
    return list(results.values())
