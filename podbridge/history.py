"""Watch history: every observed change in progress, on the source side (Patreon/YouTube) or
in Pocket Casts. Written as changes are noticed; PodBridge's own sync writes to Pocket Casts
aren't logged (they echo something already logged on the source side)."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import utcnow


def _utc(value: str | None) -> str:
    if not value:
        return utcnow()
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(conn: sqlite3.Connection, episode_id: int, side: str, position: float | None, played: bool,
        at: str | None = None) -> None:
    conn.execute("INSERT INTO watch_history (episode_id, side, position_secs, played, at) VALUES (?, ?, ?, ?, ?)",
                 (episode_id, side, position, int(played), _utc(at)))


def recently_watched_ids(conn: sqlite3.Connection, limit: int = 12) -> list[int]:
    """Episodes most recently touched on either side, newest first (hidden ones excluded)."""
    return [r[0] for r in conn.execute(
        "SELECT h.episode_id FROM watch_history h JOIN episodes e ON e.id = h.episode_id "
        "JOIN sources s ON s.id = e.source_id WHERE e.hidden = 0 AND s.enabled = 1 "
        "GROUP BY h.episode_id ORDER BY MAX(h.at) DESC LIMIT ?", (limit,))]


def by_day(conn: sqlite3.Connection, tz: str, limit: int = 300) -> "OrderedDict[str, list[dict]]":
    """History entries grouped by local day, newest first. Repeated entries for the same
    episode and side on the same day collapse into the latest one."""
    zone = ZoneInfo(tz)
    rows = conn.execute(
        "SELECT h.*, e.title, s.kind, s.label AS source_label FROM watch_history h "
        "JOIN episodes e ON e.id = h.episode_id JOIN sources s ON s.id = e.source_id "
        "WHERE e.hidden = 0 ORDER BY h.at DESC LIMIT ?", (limit,)).fetchall()
    days: OrderedDict[str, list[dict]] = OrderedDict()
    seen: set[tuple] = set()
    for row in rows:
        local = datetime.fromisoformat(row["at"].replace("Z", "+00:00")).astimezone(zone)
        day = local.strftime("%A %d %B %Y")
        key = (day, row["episode_id"], row["side"])
        if key in seen:
            continue
        seen.add(key)
        days.setdefault(day, []).append({**dict(row), "time": local.strftime("%H:%M")})
    return days
