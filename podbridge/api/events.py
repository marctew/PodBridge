"""Events (API standard section 8): what /api/v1/events streams and webhooks send.

`publish()` writes a row to the events table (and one webhook delivery per matching webhook)
from whichever thread sees it happen: the scheduler's sync, a request, a login test. Each open
stream reads the rows after the last id it sent. Rows older than an hour are pruned, always
keeping the last 200.

Item URLs need PodBridge's public address, which a background thread doesn't have: it's
remembered from real requests (`remember_base`) in the settings table.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from . import now_iso, parse

log = logging.getLogger(__name__)

EPISODE_NEW = "episode.new"
PROGRESS_SYNCED = "progress.synced"
SYNC_FINISHED = "sync.finished"
SYNC_FAILED = "sync.failed"
LOGIN_EXPIRED = "login.expired"
TYPES = [EPISODE_NEW, PROGRESS_SYNCED, SYNC_FINISHED, SYNC_FAILED, LOGIN_EXPIRED]

KEEP_S = 3600
KEEP_MIN = 200
REPLAY_MAX = 200
BASE_KEY = "api_base_url"


def remember_base(conn: sqlite3.Connection, base: str) -> None:
    """Note the address PodBridge is reached at (from a real request), for item URLs in events."""
    base = base.rstrip("/")
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (BASE_KEY,)).fetchone()
    if row is None or row[0] != base:
        with conn:
            conn.execute("INSERT INTO settings (key, value, is_secret, updated_at) VALUES (?, ?, 0, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (BASE_KEY, base, now_iso()))


def base_url(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (BASE_KEY,)).fetchone()
    return row[0] if row else ""


def publish(conn: sqlite3.Connection, type_: str, title: str, item: dict, at: str | None = None) -> int | None:
    """Record an event. Never raises: an event that can't be written mustn't break a sync."""
    from . import webhooks
    if type_ not in TYPES:
        raise ValueError(f"unknown event type {type_}")
    at = at or now_iso()
    try:
        with conn:
            event_id = conn.execute("INSERT INTO events (type, at, title, item) VALUES (?, ?, ?, ?)",
                                    (type_, at, title, json.dumps(item, separators=(",", ":")))).lastrowid
            webhooks.enqueue(conn, event_id, type_)
        return event_id
    except sqlite3.Error:
        log.exception("Couldn't record a %s event", type_)
        return None


def _row(r) -> dict:
    return {"id": r["id"], "type": r["type"], "at": r["at"], "title": r["title"], "item": json.loads(r["item"])}


def get(conn: sqlite3.Connection, event_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row(r) if r else None


def after(conn: sqlite3.Connection, last_id: int) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                                          (last_id, REPLAY_MAX))]


def last_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def prune(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute(
            "DELETE FROM events WHERE at < ? AND id <= (SELECT COALESCE(MAX(id), 0) - ? FROM events)",
            (now_iso(-KEEP_S), KEEP_MIN)).rowcount


# -- the events PodBridge publishes ------------------------------------------------------------------
# Called from syncs and requests after their own writes are committed. None of them may raise:
# an event that can't be built mustn't break what triggered it.

def _safe(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't publish %s", fn.__name__)
            return None
    return wrapper


NEW_MARK_KEY = "api_episode_mark"     # the highest episode id already announced (or seen at install)

SERVICE_NAMES = {"patreon": "Patreon", "pocketcasts": "Pocket Casts", "youtube": "YouTube"}


@_safe
def login_expired(conn: sqlite3.Connection, service: str) -> None:
    name = SERVICE_NAMES.get(service, service)
    base = base_url(conn)
    publish(conn, LOGIN_EXPIRED, f"{name} login has expired",
            {"id": f"login:{service}", "title": f"{name} login has expired", "tone": "danger",
             "url": f"{base}/settings#{service}" if base else None, "meta": {"service": service}})


def episode_item(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    """One episode in the common item shape, for events (no request needed)."""
    from .feeds import episode_row_item
    return episode_row_item(conn, episode_id, base_url(conn))


@_safe
def announce_new_episodes(conn: sqlite3.Connection) -> int:
    """episode.new for every episode added since the last call that was published in the last
    14 days (so a newly added source's back catalogue isn't announced). The first call only
    records where things stand."""
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM episodes").fetchone()[0]
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (NEW_MARK_KEY,)).fetchone()
    mark = int(row[0]) if row else None
    n = 0
    if mark is not None:
        for r in conn.execute("SELECT e.id, e.published_at FROM episodes e JOIN sources s ON s.id = e.source_id "
                              "WHERE e.id > ? AND e.hidden = 0 AND s.enabled = 1 ORDER BY e.published_at, e.id",
                              (mark,)).fetchall():
            if is_recent(r["published_at"]):
                item = episode_item(conn, r["id"])
                if item:
                    publish(conn, EPISODE_NEW, f"New: {item['title']}", item)
                    n += 1
    if mark != top:
        with conn:
            conn.execute("INSERT INTO settings (key, value, is_secret, updated_at) VALUES (?, ?, 0, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (NEW_MARK_KEY, str(top), now_iso()))
    return n


@_safe
def progress_synced(conn: sqlite3.Connection, episode_id: int, action: str, position: float | None) -> None:
    item = episode_item(conn, episode_id)
    if not item:
        return
    item["meta"] = {**item.get("meta", {}), "action": action, "position_secs": position}
    verb = "Marked played" if action == "mark_played" else "Position written"
    publish(conn, PROGRESS_SYNCED, f"{verb}: {item['title']}", item)


@_safe
def sync_done(conn: sqlite3.Connection, run_id: int, status: str, checked: int, updated: int,
              error: str | None, dry_run: bool) -> None:
    from .feeds import sync_item
    row = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return
    item = sync_item(row, base_url(conn))
    if status == "ok":
        publish(conn, SYNC_FINISHED, item["title"], item)
    else:
        publish(conn, SYNC_FAILED, item["title"], item)


def is_recent(published_at: str | None, days: int = 14) -> bool:
    from datetime import datetime, timedelta, timezone
    moment = parse(published_at)
    return bool(moment and datetime.now(timezone.utc) - moment <= timedelta(days=days))
