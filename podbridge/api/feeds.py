"""/api/v1/status and the feeds (API standard section 5), built from PodBridge's library.

Every item is in the common shape: id, title, subtitle, at (UTC) with at_label (local), tone,
image (a path on this app), url (the page in PodBridge) and meta (PodBridge's own fields, listed
per feed in docs/API.md).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from .. import library, my_list
from ..alerts import current_problems
from ..matching import strip_channel_suffix
from ..settings_store import DEFAULTS
from . import iso, now_iso, parse

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
STATUS_REFRESH_S = 60


@dataclass(frozen=True)
class Feed:
    name: str
    title: str
    refresh_s: int
    timed: bool          # ordered by time, newest first: takes since and cursor


FEEDS = {f.name: f for f in (
    Feed("continue", "Continue watching", 120, False),
    Feed("new", "New episodes", 600, True),
    Feed("up_next", "Up next", 600, False),
    Feed("my_list", "My List", 300, False),
    Feed("history", "Recently watched", 120, True),
    Feed("syncs", "Syncs", 120, True),
)}


# -- helpers -------------------------------------------------------------------------------------------

def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(current_app.config["PODBRIDGE"].tz)
    except RuntimeError:           # no app context (a sync run straight from a test or script)
        return ZoneInfo("Europe/London")


def at_label(value: str | None) -> str | None:
    """'Today 19:12', 'Yesterday 08:05', 'Sat 7 Nov', '7 Nov 2025': the moment, for a person."""
    moment = parse(value)
    if moment is None:
        return None
    zone = _tz()
    local = moment.astimezone(zone)
    today = datetime.now(zone).date()
    if local.date() == today:
        return f"Today {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {local:%H:%M}"
    if (today - local.date()).days < 7 and local.date() <= today:
        return f"{local:%a} {local.day} {local:%b}"
    if local.year == today.year:
        return f"{local.day} {local:%b}"
    return f"{local.day} {local:%b %Y}"


def _clock(secs: float | None) -> str:
    total = int(secs or 0)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _param(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'patreon_timestamp_param'").fetchone()
    return row[0] if row else DEFAULTS["patreon_timestamp_param"]


def _clean(d: dict) -> dict:
    out = {k: v for k, v in d.items() if v is not None}
    if isinstance(out.get("meta"), dict):
        out["meta"] = {k: v for k, v in out["meta"].items() if v is not None}
    return out


def utc(value: str | None) -> str | None:
    """Any stored time ('2026-09-30T23:00:00.000+00:00', '...Z') as the standard's UTC '...Z'."""
    moment = parse(value)
    return iso(moment) if moment else None


def episode_item(ep: dict, base: str, at: str | None = None) -> dict:
    """An annotated library episode (library.annotate) as a feed item."""
    at = utc(at)
    resume = ep.get("resume")
    position = resume.position_secs if resume else None
    left = (ep["duration_secs"] - position) if (position and ep.get("duration_secs")) else None
    show = ep.get("show_title") or (ep.get("source_label") or "").removeprefix("YouTube: ")
    detail = {"in_progress": f"{_clock(left)} left" if left else "In progress",
              "played": "Played", "unwatched": "New" if ep.get("is_new") else None}.get(ep["state"])
    return _clean({
        "id": f"episode:{ep['id']}",
        "title": strip_channel_suffix(ep["title"]),
        "subtitle": " · ".join(x for x in (show, detail) if x),
        "at": at,
        "at_label": at_label(at),
        "tone": "accent" if ep["state"] == "in_progress" else None,
        "image": f"/api/v1/images/episode/{ep['id']}",
        "url": f"{base}/episodes/{ep['id']}",
        "meta": {
            "episode_id": ep["id"], "show": show, "source": ep.get("source_kind"), "state": ep["state"],
            "progress_pct": round(ep.get("progress_pct") or 0.0, 1),
            "position_secs": round(position, 1) if position is not None else None,
            "duration_secs": ep.get("duration_secs"), "published_at": utc(ep.get("published_at")),
            "resume_url": ep.get("resume_url"), "pocketcasts_url": ep.get("pc_web_url"),
            "matched": bool(ep.get("pocketcasts_episode_uuid")), "in_list": bool(ep.get("in_list")),
        },
    })


def episode_row_item(conn: sqlite3.Connection, episode_id: int, base: str) -> dict | None:
    """One episode by id, without building the whole library (for events)."""
    row = conn.execute(library.EPISODE_QUERY + " WHERE e.id = ?", (episode_id,)).fetchone()
    if row is None:
        return None
    ep = library.annotate(row, _param(conn))
    title = conn.execute("SELECT title FROM source_podcasts WHERE source_id = ? AND podcast_uuid = ?",
                         (ep["source_id"], ep.get("matched_podcast_uuid"))).fetchone()
    if title and title[0]:
        ep["show_title"] = title[0]
    return episode_item(ep, base, ep.get("published_at"))


def _touched(ep: dict) -> str | None:
    return iso(ep["touched"]) if ep.get("touched") else None


def sync_item(row, base: str) -> dict:
    status = row["status"]
    dry = " (dry run)" if row["dry_run"] else ""
    if status == "ok":
        title = (f"Synced{dry}: {row['episodes_updated']} written to Pocket Casts" if row["episodes_updated"]
                 else f"Synced{dry}: nothing to write")
    elif status == "running":
        title = "Syncing…"
    else:
        title = f"Sync {'stopped' if status == 'aborted' else 'failed'}: {(row['error'] or 'unknown error')[:120]}"
    return _clean({
        "id": f"sync:{row['id']}", "title": title,
        "subtitle": f"{row['episodes_checked']} checked" + (f" · {row['tier']}" if row["tier"] != "full" else ""),
        "at": row["started_at"], "at_label": at_label(row["started_at"]),
        "tone": {"ok": "success", "running": "info", "aborted": "warning"}.get(status, "danger"),
        "url": f"{base}/activity",
        "meta": {"run_id": row["id"], "status": status, "dry_run": bool(row["dry_run"]), "tier": row["tier"],
                 "checked": row["episodes_checked"], "updated": row["episodes_updated"],
                 "finished_at": row["finished_at"], "error": row["error"]},
    })


# -- status ----------------------------------------------------------------------------------------------

def _counts(conn: sqlite3.Connection, shows: list) -> dict:
    zone = _tz()
    midnight = iso(datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0))
    unmatched = conn.execute(
        "SELECT COUNT(*) FROM episodes e JOIN sources s ON s.id = e.source_id WHERE s.enabled = 1 "
        "AND e.match_method = 'none' AND e.match_locked = 0 AND e.hidden = 0 "
        "AND s.pocketcasts_podcast_uuid IS NOT NULL").fetchone()[0]
    written = conn.execute(
        "SELECT COUNT(*) FROM sync_events ev JOIN sync_runs r ON r.id = ev.run_id WHERE r.dry_run = 0 "
        "AND ev.action IN ('set_position', 'mark_played') AND r.started_at >= ?", (midnight,)).fetchone()[0]
    return {
        "in_progress": len(library.continue_watching(shows, None)),
        "new_this_week": len(library.new_this_week(shows, limit=10_000)),
        "my_list": len(my_list.ids(conn)),
        "unmatched": unmatched,
        "written_today": written,
    }


def status(conn: sqlite3.Connection, store, base: str, busy: bool) -> dict:
    shows = library.build_shows(conn, _param(conn))
    c = _counts(conn, shows)
    last = conn.execute("SELECT * FROM sync_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1").fetchone()
    sources = conn.execute("SELECT COUNT(*) FROM sources WHERE enabled = 1").fetchone()[0]
    problems = current_problems(conn, store)
    in_progress = f"{c['in_progress']} in progress"
    since = f"last sync {at_label(last['started_at']).lower()}" if last else "not synced yet"
    if busy:
        state, headline = "busy", f"Syncing now · {in_progress}"
    elif not (store.is_set("pocketcasts_email") and sources):
        state, headline = "off", "Not set up yet: add Pocket Casts and a source in Settings."
    elif any(p.key == "sync" for p in problems):
        state, headline = "error", next(p.text for p in problems if p.key == "sync")
    elif problems:
        state, headline = "warning", problems[0].text
    else:
        state, headline = "ok", f"{in_progress} · {since}"
    top = library.continue_watching(shows, 1)
    detail = f"Next: {strip_channel_suffix(top[0]['title'])}" if top else None
    if store.get_bool("dry_run"):
        detail = "Dry run is on: nothing is written to Pocket Casts." + (f" {detail}" if detail else "")
    counts = [
        {"key": "in_progress", "label": "In progress", "value": c["in_progress"]},
        {"key": "new_this_week", "label": "New this week", "value": c["new_this_week"]},
        {"key": "my_list", "label": "My List", "value": c["my_list"]},
        {"key": "unmatched", "label": "Unmatched", "value": c["unmatched"],
         **({"tone": "warning"} if c["unmatched"] else {})},
        {"key": "written_today", "label": "Written today", "value": c["written_today"]},
    ]
    return _clean({"app": "podbridge", "state": state, "headline": headline, "detail": detail, "counts": counts,
                   "url": f"{base}/", "generated_at": now_iso(), "refresh_s": STATUS_REFRESH_S})


# -- feeds -----------------------------------------------------------------------------------------------

def feed(conn: sqlite3.Connection, name: str, base: str, limit: int = DEFAULT_LIMIT,
         since: str | None = None, cursor: str | None = None) -> dict:
    spec = FEEDS[name]
    limit = max(1, min(MAX_LIMIT, limit))
    if name == "history":
        items = _history(conn, base, limit + 1, since, cursor)
    elif name == "syncs":
        items = _syncs(conn, base, limit + 1, since, cursor)
    else:
        shows = library.build_shows(conn, _param(conn))
        if name == "continue":
            items = [episode_item(e, base, _touched(e)) for e in library.continue_watching(shows, limit)]
        elif name == "up_next":
            items = [episode_item(e, base, e.get("published_at")) for e in library.up_next(shows, limit)]
        elif name == "my_list":
            items = [episode_item(e, base, e.get("published_at"))
                     for e in library.in_order(shows, my_list.ids(conn))[:limit]]
        else:  # new: newest published first
            eps = [(parse(e.get("published_at")), e) for e in library.all_episodes(shows)]
            eps = [(when, e) for when, e in eps if when is not None]
            eps.sort(key=lambda pair: pair[0], reverse=True)
            if since:
                eps = [(w, e) for w, e in eps if w > parse(since)]
            if cursor:
                eps = [(w, e) for w, e in eps if w < parse(cursor)]
            eps = [e for _, e in eps]
            items = [episode_item(e, base, e["published_at"]) for e in eps[:limit + 1]]
    next_cursor = None
    if spec.timed and len(items) > limit:
        items = items[:limit]
        next_cursor = items[-1]["at"]
    return {"feed": name, "title": spec.title, "items": items[:limit], "next_cursor": next_cursor,
            "generated_at": now_iso(), "refresh_s": spec.refresh_s}


def _window(since: str | None, cursor: str | None, column: str) -> tuple[str, list]:
    where, args = "", []
    if since:
        where += f" AND {column} > ?"
        args.append(since)
    if cursor:
        where += f" AND {column} < ?"
        args.append(cursor)
    return where, args


def _history(conn, base: str, limit: int, since, cursor) -> list[dict]:
    where, args = _window(since, cursor, "h.at")
    rows = conn.execute(
        "SELECT h.id AS history_id, h.side, h.position_secs AS h_position, h.played AS h_played, h.at "
        "AS h_at, h.episode_id FROM watch_history h JOIN episodes e ON e.id = h.episode_id "
        f"JOIN sources s ON s.id = e.source_id WHERE e.hidden = 0 AND s.enabled = 1{where} "
        "ORDER BY h.at DESC, h.id DESC LIMIT ?", (*args, limit)).fetchall()
    side_names = {"patreon": "Patreon", "youtube": "YouTube", "pocketcasts": "Pocket Casts"}
    out = []
    for r in rows:
        item = episode_row_item(conn, r["episode_id"], base)
        if item is None:
            continue
        what = "played" if r["h_played"] else _clock(r["h_position"])
        item.update(id=f"history:{r['history_id']}", at=r["h_at"], at_label=at_label(r["h_at"]),
                    subtitle=f"{item['meta']['show']} · {side_names.get(r['side'], r['side'])} · {what}")
        item["meta"].update(side=r["side"], watched_position_secs=r["h_position"], watched_played=bool(r["h_played"]))
        out.append(item)
    return out


def _syncs(conn, base: str, limit: int, since, cursor) -> list[dict]:
    where, args = _window(since, cursor, "started_at")
    rows = conn.execute(f"SELECT * FROM sync_runs WHERE 1 = 1{where} ORDER BY started_at DESC, id DESC LIMIT ?",
                        (*args, limit)).fetchall()
    return [sync_item(r, base) for r in rows]
