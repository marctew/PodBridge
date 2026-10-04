"""Watching/listening statistics, derived from watch_history.

Time is counted from how far each episode's *furthest* position moved, whichever app moved
it: watching to 40:00 on Patreon then listening 40:00 -> 45:00 in Pocket Casts is 45 minutes,
not 85. Rewatching earlier parts adds nothing. PodBridge's own sync writes aren't in the
history, so syncing never counts as listening.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SIDES = ("patreon", "youtube", "pocketcasts")   # fixed order: colour follows the app, never the rank
SIDE_LABELS = {"patreon": "Patreon", "youtube": "YouTube", "pocketcasts": "Pocket Casts"}
MAX_JUMP_SECS = 4 * 3600   # a single jump bigger than this is a data oddity, not listening


@dataclass(frozen=True)
class Segment:
    episode_id: int
    side: str
    secs: float
    at: datetime          # local time
    finished: bool


def segments(conn: sqlite3.Connection, tz: str) -> list[Segment]:
    zone = ZoneInfo(tz)
    rows = conn.execute(
        "SELECT h.episode_id, h.side, h.position_secs, h.played, h.at, e.duration_secs "
        "FROM watch_history h JOIN episodes e ON e.id = h.episode_id ORDER BY h.episode_id, h.at, h.id").fetchall()
    out: list[Segment] = []
    furthest: dict[int, float] = defaultdict(float)
    for r in rows:
        position = r["position_secs"]
        if r["played"] and r["duration_secs"] and (position is None or position < r["duration_secs"]):
            position = r["duration_secs"]
        position = position or 0.0
        gain = position - furthest[r["episode_id"]]
        if gain > 0:
            furthest[r["episode_id"]] = position
        at = datetime.fromisoformat(r["at"].replace("Z", "+00:00")).astimezone(zone)
        out.append(Segment(r["episode_id"], r["side"], min(max(gain, 0.0), MAX_JUMP_SECS), at, bool(r["played"])))
    return out


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to 1, 2, 2.5 or 5 x 10^k."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** len(str(int(value))) / 10
    for step in (1, 2, 2.5, 5, 10):
        if step * magnitude >= value:
            return step * magnitude
    return 10 * magnitude


def _streaks(days: set[date], today: date) -> tuple[int, int]:
    """(current, longest) runs of consecutive active days. Current counts from today, or
    yesterday if nothing yet today."""
    longest = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous and d - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = d
    current = 0
    cursor = today if today in days else today - timedelta(days=1)
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def summarize(conn: sqlite3.Connection, tz: str, show_titles: dict[int, str], weeks: int = 12,
              now: datetime | None = None) -> dict:
    zone = ZoneInfo(tz)
    now = (now or datetime.now(zone)).astimezone(zone)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())   # Monday
    month_ago = now - timedelta(days=30)
    segs = segments(conn, tz)

    def total(items) -> float:
        return sum(s.secs for s in items)

    this_week = [s for s in segs if s.at.date() >= week_start]
    last_30 = [s for s in segs if s.at >= month_ago]
    finished_30 = {s.episode_id for s in last_30 if s.finished}
    finished_all = {s.episode_id for s in segs if s.finished}
    started_all = {s.episode_id for s in segs if s.secs > 0 or s.finished}
    active_days = {s.at.date() for s in segs if s.secs > 0}
    current_streak, longest_streak = _streaks(active_days, today)

    weekly = []
    for i in range(weeks - 1, -1, -1):
        start = week_start - timedelta(weeks=i)
        end = start + timedelta(days=7)
        by_side = {side: total(s for s in segs if s.side == side and start <= s.at.date() < end) for side in SIDES}
        weekly.append({"start": start, "label": start.strftime("%d %b"), "by_side": by_side,
                       "total": sum(by_side.values()), "current": i == 0})
    # Four equal steps of a clean size (0.5h, 1h, 2h, 5h…), so ticks read 0 / 1h / 2h / 3h / 4h.
    raw_step = max((w["total"] for w in weekly), default=0) / 3600 / 4
    step = 0.25 if raw_step <= 0.25 else 0.5 if raw_step <= 0.5 else _nice_ceiling(raw_step)
    axis_max = step * 4 * 3600

    per_show: dict[str, dict] = defaultdict(lambda: {"secs_30": 0.0, "secs_all": 0.0, "finished": set()})
    for s in segs:
        title = show_titles.get(s.episode_id, "Other")
        per_show[title]["secs_all"] += s.secs
        if s.at >= month_ago:
            per_show[title]["secs_30"] += s.secs
        if s.finished:
            per_show[title]["finished"].add(s.episode_id)
    shows = sorted(({"title": t, "secs_30": v["secs_30"], "secs_all": v["secs_all"], "finished": len(v["finished"])}
                    for t, v in per_show.items() if v["secs_all"] > 0),
                   key=lambda r: (r["secs_30"], r["secs_all"]), reverse=True)[:10]
    show_max = max((r["secs_30"] for r in shows), default=0)

    split_30 = {side: total(s for s in last_30 if s.side == side) for side in SIDES}
    return {
        "has_data": bool(segs),
        "this_week": total(this_week), "last_30": total(last_30), "all_time": total(segs),
        "finished_30": len(finished_30), "finished_all": len(finished_all),
        "completion": (len(finished_all) / len(started_all)) if started_all else None,
        "current_streak": current_streak, "longest_streak": longest_streak,
        "weekly": weekly, "axis_max": axis_max,
        "axis_ticks": [axis_max * f for f in (0, .25, .5, .75, 1)],
        "shows": shows, "show_max": show_max,
        "split_30": split_30, "split_30_total": sum(split_30.values()),
        "first_day": min((s.at.date() for s in segs), default=None),
    }
