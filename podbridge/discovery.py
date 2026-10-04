"""Patreon session health check and episode discovery into the database.

One list sweep per source returns every post with its media ID and progress
(Phase 0 finding), so discovery also refreshes the Patreon side of `progress`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import artwork, history
from .rules import interpret_patreon

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


def upsert_post(conn: sqlite3.Connection, source_id: int, post: Post, with_progress: bool = True) -> bool:
    """Insert or refresh one episode and (unless with_progress is False) its source-side progress.
    Unknown dates/lengths never overwrite known ones. Returns True if newly added."""
    existing = conn.execute(
        "SELECT id FROM episodes WHERE source_id = ? AND patreon_post_id = ?", (source_id, post.post_id)
    ).fetchone()
    now = utcnow()
    if existing:
        episode_id = existing["id"]
        conn.execute(
            "UPDATE episodes SET patreon_media_id = ?, patreon_url = ?, title = ?, "
            "published_at = COALESCE(?, published_at), duration_secs = COALESCE(?, duration_secs), "
            "updated_at = ? WHERE id = ?",
            (post.media_id, post.url, post.title, post.published_at, post.duration_secs, now, episode_id),
        )
    else:
        episode_id = conn.execute(
            "INSERT INTO episodes (source_id, patreon_post_id, patreon_media_id, patreon_url, title, "
            "published_at, duration_secs) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, post.post_id, post.media_id, post.url, post.title, post.published_at,
             post.duration_secs),
        ).lastrowid
    if not with_progress:
        return existing is None
    p = post.progress
    previous = conn.execute("SELECT patreon_updated_at FROM progress WHERE episode_id = ?", (episode_id,)).fetchone()
    if p.updated_at and (previous is None or previous[0] != p.updated_at):
        # "Finished" by the same rule as syncing: Patreon's own watched flag fires early.
        finished = interpret_patreon(p.position_secs, p.is_watched, post.duration_secs).played
        history.log(conn, episode_id, "youtube" if post.post_type == "youtube" else "patreon",
                    p.position_secs, finished, at=p.updated_at)
    conn.execute(
        "INSERT INTO progress (episode_id, patreon_position_secs, patreon_is_watched, patreon_watch_state, "
        "patreon_updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (episode_id) DO UPDATE SET patreon_position_secs = excluded.patreon_position_secs, "
        "patreon_is_watched = excluded.patreon_is_watched, patreon_watch_state = excluded.patreon_watch_state, "
        "patreon_updated_at = excluded.patreon_updated_at",
        (episode_id, p.position_secs, int(p.is_watched), p.watch_state, p.updated_at),
    )
    return existing is None


def discover_source(conn: sqlite3.Connection, client: PatreonClient, source: sqlite3.Row,
                    art_dir: Path | None = None, art_budget: list[int] | None = None) -> DiscoveryResult:
    result = DiscoveryResult(source_id=source["id"], label=source["label"])
    posts = client.list_posts(source["campaign_id"], source["collection_id"] or None)
    kept = []
    with conn:
        for post in posts:
            result.posts_seen += 1
            if not post.media_id:
                result.skipped_no_media += 1
                continue
            kept.append(post)
            if upsert_post(conn, source["id"], post):
                result.added += 1
            else:
                result.refreshed += 1
    if art_dir is not None:
        _download_thumbnails(conn, art_dir, kept, art_budget if art_budget is not None
                             else [artwork.MAX_PATREON_DOWNLOADS_PER_RUN])
    log.info("Discovery for source %s: %s", source["id"], result)
    return result


def _download_thumbnails(conn: sqlite3.Connection, art_dir: Path, posts: list[Post], budget: list[int]) -> None:
    """Patreon thumbnail URLs are signed and expire, so they're fetched now or never."""
    for post in posts:
        if budget[0] <= 0:
            return
        key = f"patreon:{post.post_id}"
        if not post.thumbnail_url or artwork.has_art(conn, key):
            continue
        budget[0] -= 1
        artwork.ensure(conn, art_dir, key, [post.thumbnail_url])


def discover_all(conn: sqlite3.Connection, store: SettingsStore, client: PatreonClient,
                 art_dir: Path | None = None) -> list[DiscoveryResult]:
    """Health check first: a dead session returns logged-out defaults that look like real data."""
    if not check_patreon_session(store, client):
        raise PatreonSessionExpired("Patreon session is not logged in")
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 AND kind = 'patreon' ORDER BY id").fetchall()
    budget = [artwork.MAX_PATREON_DOWNLOADS_PER_RUN]
    return [discover_source(conn, client, source, art_dir, budget) for source in sources]


# --- YouTube ---

def has_sources(conn: sqlite3.Connection, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM sources WHERE enabled = 1 AND kind = ?", (kind,)).fetchone() is not None


def check_youtube_session(store: SettingsStore, client) -> bool:
    ok = client.check_session()
    store.set("youtube_status", "ok" if ok else "expired")
    store.set("youtube_verified_at", utcnow())
    if not ok:
        note_youtube_expiry(store)
    return ok


def note_youtube_expiry(store: SettingsStore) -> None:
    """Remember when the pasted YouTube login first stopped working, to show how long it lasted."""
    if not store.get("youtube_expired_at"):
        store.set("youtube_expired_at", utcnow())


DETAILS_RETRY_AFTER = timedelta(days=1)


MAX_DETAIL_FETCHES_PER_RUN = 25  # interactive runs: watch pages are ~1.5 s each
BACKGROUND_DETAIL_FETCHES = 400  # scheduled runs aren't waited on, so fetch everything missing


def _cached_details(conn: sqlite3.Connection, client, video_id: str, budget: list[int] | None = None,
                    retry_failed: bool = False):
    """Watch-page details, fetched once. A failed lookup is cached too, and retried after a day
    (or straight away with retry_failed). `budget` is a one-item list counting fetches left this
    run; when it's spent, the cached row (possibly None) is returned without fetching."""
    from .youtube import YouTubeBlocked, YouTubeError, YouTubeSessionExpired

    row = conn.execute("SELECT * FROM youtube_videos WHERE video_id = ?", (video_id,)).fetchone()
    if row is not None:
        fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if row["published_at"] or (not retry_failed and (
                row["channel_id"] or datetime.now(timezone.utc) - fetched < DETAILS_RETRY_AFTER)):
            return row
    if budget is not None:
        if budget[0] <= 0:
            return row
        budget[0] -= 1
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


def _youtube_progress(conn, source_id: int, video_id: str, percent: float, duration: float | None) -> Progress:
    """Position = percent watched x length. YouTube gives no 'last watched' time, so the
    timestamp is 'now' whenever the position changes, and kept otherwise."""
    position = round(percent / 100 * duration, 1) if duration else None
    existing = conn.execute(
        "SELECT p.patreon_position_secs, p.patreon_updated_at FROM episodes e "
        "JOIN progress p ON p.episode_id = e.id WHERE e.source_id = ? AND e.patreon_post_id = ?",
        (source_id, video_id)).fetchone()
    unchanged = existing is not None and existing["patreon_position_secs"] == position
    updated_at = existing["patreon_updated_at"] if unchanged else utcnow()
    return Progress(position_secs=position, is_watched=False,
                    watch_state="is_watching" if position else "is_not_watched",
                    updated_at=updated_at if position else None)


def _store_video(conn, source, result: DiscoveryResult, video_id: str, title: str, published_at: str | None,
                 duration: float | None, percent: float | None) -> None:
    from .youtube import watch_url

    progress = (_youtube_progress(conn, source["id"], video_id, percent, duration) if percent is not None
                else Progress(None, False, "is_not_watched", None))
    post = Post(post_id=video_id, title=title, published_at=published_at, url=watch_url(video_id),
                post_type="youtube", media_id=video_id, duration_secs=duration, progress=progress)
    with conn:
        # Only history carries progress. A video that isn't in this run's history keeps whatever
        # progress was recorded before (it may simply have scrolled off the first history page).
        if upsert_post(conn, source["id"], post, with_progress=percent is not None):
            result.added += 1
        else:
            result.refreshed += 1


def missing_youtube_dates(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM episodes e JOIN sources s ON s.id = e.source_id "
        "WHERE s.kind = 'youtube' AND s.enabled = 1 AND e.published_at IS NULL").fetchone()[0]


def backfill_youtube_dates(conn: sqlite3.Connection, public_client, budget: int | None = None) -> int:
    """Fetch publish dates (public watch pages, no login) for YouTube episodes that lack one,
    newest uploads first. Returns how many dates were filled in."""
    rows = conn.execute(
        "SELECT e.id, e.patreon_post_id FROM episodes e JOIN sources s ON s.id = e.source_id "
        "WHERE s.kind = 'youtube' AND s.enabled = 1 AND e.published_at IS NULL ORDER BY e.id").fetchall()
    remaining = [budget if budget is not None else len(rows)]
    filled = 0
    for row in rows:
        if remaining[0] <= 0:
            break
        # An explicit backfill retries earlier failures rather than waiting a day.
        details = _cached_details(conn, public_client, row["patreon_post_id"], remaining, retry_failed=True)
        if details is not None and details["published_at"]:
            with conn:
                conn.execute("UPDATE episodes SET published_at = ?, duration_secs = COALESCE(duration_secs, ?) "
                             "WHERE id = ?", (details["published_at"], details["duration_secs"], row["id"]))
            filled += 1
    return filled


def refresh_youtube_episode(conn: sqlite3.Connection, store: SettingsStore, client, episode) -> bool:
    """Re-read one YouTube episode's progress from watch history. Returns False if the video
    isn't on the first history page (its stored progress is kept)."""
    from .youtube import YouTubeSessionExpired, watch_url

    if not check_youtube_session(store, client):
        raise YouTubeSessionExpired("YouTube cookies are no longer signed in")
    item = next((i for i in client.history() if i.video_id == episode["patreon_post_id"]), None)
    if item is None or item.percent is None:
        return False
    progress = _youtube_progress(conn, episode["source_id"], item.video_id, item.percent, episode["duration_secs"])
    post = Post(post_id=item.video_id, title=episode["title"], published_at=episode["published_at"],
                url=watch_url(item.video_id), post_type="youtube", media_id=item.video_id,
                duration_secs=episode["duration_secs"], progress=progress)
    with conn:
        upsert_post(conn, episode["source_id"], post)
    return True


def discover_youtube_all(conn: sqlite3.Connection, store: SettingsStore, client,
                         detail_budget: int | None = None) -> list[DiscoveryResult]:
    """Every recent upload of each enabled YouTube channel source (about 100, from the uploads
    playlist), with progress from the first page of watch history.

    Exact publish dates need one watch-page request per video; at most
    MAX_DETAIL_FETCHES_PER_RUN are made per run and later runs fill in the rest.
    """
    from .youtube import YouTubeSessionExpired

    if not check_youtube_session(store, client):
        raise YouTubeSessionExpired("YouTube cookies are no longer signed in")
    sources = {s["campaign_id"]: s for s in conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND kind = 'youtube' ORDER BY id")}
    results = {cid: DiscoveryResult(source_id=s["id"], label=s["label"]) for cid, s in sources.items()}
    if not sources:
        return []
    history = {i.video_id: i for i in client.history() if i.percent is not None}
    budget = [MAX_DETAIL_FETCHES_PER_RUN if detail_budget is None else detail_budget]

    def cached(video_id: str):
        return conn.execute("SELECT * FROM youtube_videos WHERE video_id = ?", (video_id,)).fetchone()

    # 1. Each channel's uploads; progress where the video is in history.
    listed: dict[str, str] = {}
    for channel_id, source in sources.items():
        uploads = client.channel_videos(channel_id)
        results[channel_id].posts_seen = len(uploads)
        for item in uploads:
            listed[item.video_id] = channel_id
            row = cached(item.video_id)
            duration = item.duration_secs or (row["duration_secs"] if row else None)
            percent = history[item.video_id].percent if item.video_id in history else None
            _store_video(conn, source, results[channel_id], item.video_id, item.title,
                         row["published_at"] if row else None, duration, percent)

    # 2. Exact publish dates for listed videos, newest first, within the per-run budget.
    for video_id, channel_id in listed.items():
        row = cached(video_id)
        if row is not None and row["published_at"]:
            continue
        row = _cached_details(conn, client, video_id, budget)
        if row is not None and row["published_at"]:
            with conn:
                conn.execute("UPDATE episodes SET published_at = ?, duration_secs = COALESCE(duration_secs, ?) "
                             "WHERE source_id = ? AND patreon_post_id = ?",
                             (row["published_at"], row["duration_secs"], sources[channel_id]["id"], video_id))
        if budget[0] <= 0:
            break

    # 3. Watched videos older than the uploads page: the watch page says which channel they're from.
    for video_id, item in history.items():
        if video_id in listed:
            continue
        row = _cached_details(conn, client, video_id, budget)
        if row is None or row["channel_id"] not in sources:
            continue
        source = sources[row["channel_id"]]
        _store_video(conn, source, results[row["channel_id"]], video_id, row["title"] or item.title,
                     row["published_at"], row["duration_secs"], item.percent)
    return list(results.values())
