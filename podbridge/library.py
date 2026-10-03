"""The Library view of the data: shows (one per linked Pocket Casts podcast) and their episodes,
with resume positions, progress and artwork keys for the Plex-style pages."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .matching import route_by_suffix
from .resume import build_resume_url, last_touched, resume_position

EPISODE_QUERY = (
    "SELECT e.*, s.pocketcasts_podcast_uuid, s.enabled AS source_enabled, s.kind AS source_kind, "
    "s.label AS source_label, pe.podcast_uuid AS matched_podcast_uuid, "
    "p.patreon_position_secs, p.patreon_is_watched, p.patreon_watch_state, p.patreon_updated_at, "
    "p.pocketcasts_position_secs, p.pocketcasts_status, p.pocketcasts_changed_at, p.last_synced_at "
    "FROM episodes e JOIN sources s ON s.id = e.source_id LEFT JOIN progress p ON p.episode_id = e.id "
    "LEFT JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid"
)


def annotate(row, param: str) -> dict:
    """Row -> dict with resume position, Continue URL, progress, state and thumbnail key."""
    ep = dict(row)
    youtube = ep.get("source_kind") == "youtube"
    ep["source_name"] = "YouTube" if youtube else "Patreon"
    ep["resume"] = resume_position(ep["patreon_position_secs"], bool(ep["patreon_is_watched"]),
                                   ep["duration_secs"], ep["pocketcasts_status"], ep["pocketcasts_position_secs"])
    ep["resume_url"] = (build_resume_url(ep["patreon_url"], ep["resume"].position_secs, "t" if youtube else param)
                        if ep["resume"] and ep["patreon_url"] else None)
    ep["touched"] = last_touched(ep["patreon_updated_at"], ep["pocketcasts_changed_at"])
    if ep["resume"] is None:
        ep["state"], ep["progress_pct"] = "played", 100.0
    elif ep["resume"].position_secs > 0:
        ep["state"] = "in_progress"
        ep["progress_pct"] = (min(99.0, ep["resume"].position_secs / ep["duration_secs"] * 100)
                              if ep["duration_secs"] else 0.0)
    else:
        ep["state"], ep["progress_pct"] = "unwatched", 0.0
    ep["thumb_key"] = f"{'yt' if youtube else 'patreon'}:{ep['patreon_post_id']}"
    return ep


@dataclass
class Show:
    source_id: int
    podcast_uuid: str | None
    title: str
    source_label: str
    kind: str
    episodes: list[dict] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.podcast_uuid or "-"

    @property
    def art_key(self) -> str | None:
        return f"podcast:{self.podcast_uuid}" if self.podcast_uuid else None

    @property
    def in_progress(self) -> int:
        return sum(1 for e in self.episodes if e["state"] == "in_progress")

    @property
    def unwatched(self) -> int:
        return sum(1 for e in self.episodes if e["state"] == "unwatched")

    @property
    def latest(self) -> str:
        return max((e["published_at"] or "" for e in self.episodes), default="")


def build_shows(conn: sqlite3.Connection, param: str) -> list[Show]:
    """One show per (enabled source, linked podcast); an unlinked source is one show of its own.
    Matched episodes go to their podcast's show. Unmatched ones go to the podcast their
    '| suffix' names, else the source's first podcast."""
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY id").fetchall()
    links: dict[int, dict[str, str | None]] = {}
    for row in conn.execute("SELECT source_id, podcast_uuid, title FROM source_podcasts ORDER BY rowid"):
        links.setdefault(row["source_id"], {})[row["podcast_uuid"]] = row["title"]

    shows: dict[tuple[int, str | None], Show] = {}
    for s in sources:
        podcasts = links.get(s["id"]) or {None: None}
        for uuid, title in podcasts.items():
            name = title or s["label"].removeprefix("YouTube: ")
            shows[(s["id"], uuid)] = Show(s["id"], uuid, name, s["label"], s["kind"])

    rows = conn.execute(EPISODE_QUERY + " WHERE s.enabled = 1 ORDER BY e.published_at DESC").fetchall()
    for row in rows:
        ep = annotate(row, param)
        podcasts = links.get(ep["source_id"]) or {None: None}
        target = ep["matched_podcast_uuid"] if ep["matched_podcast_uuid"] in podcasts else None
        if target is None:
            target = route_by_suffix(ep["title"], podcasts) if None not in podcasts else None
        if target is None:
            target = next(iter(podcasts))
        show = shows.get((ep["source_id"], target))
        if show is not None:
            ep["show_title"] = show.title
            ep["show_art_key"] = show.art_key
            show.episodes.append(ep)
    return sorted(shows.values(), key=lambda sh: sh.latest, reverse=True)


def all_episodes(shows: list[Show]) -> list[dict]:
    return [ep for show in shows for ep in show.episodes]


def continue_watching(shows: list[Show], limit: int | None = 12) -> list[dict]:
    eps = [e for e in all_episodes(shows) if e["state"] == "in_progress" and e["resume_url"]]
    eps.sort(key=lambda e: e["touched"].timestamp() if e["touched"] else 0, reverse=True)
    return eps[:limit] if limit else eps


def recently_added(shows: list[Show], limit: int = 16) -> list[dict]:
    eps = [e for e in all_episodes(shows) if e["published_at"]]
    eps.sort(key=lambda e: _ts(e["published_at"]), reverse=True)
    return eps[:limit]


def _ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
