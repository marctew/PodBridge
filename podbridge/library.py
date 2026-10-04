"""The Library view of the data: shows (one per linked Pocket Casts podcast) and their episodes,
with resume positions, progress and artwork keys for the Plex-style pages."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .matching import route_by_suffix
from .resume import build_resume_url, last_touched, parse_time, resume_position

EPISODE_QUERY = (
    "SELECT e.*, s.pocketcasts_podcast_uuid, s.enabled AS source_enabled, s.kind AS source_kind, "
    "s.label AS source_label, pe.podcast_uuid AS matched_podcast_uuid, "
    "p.patreon_position_secs, p.patreon_is_watched, p.patreon_watch_state, p.patreon_updated_at, "
    "p.pocketcasts_position_secs, p.pocketcasts_status, p.pocketcasts_changed_at, p.last_synced_at "
    "FROM episodes e JOIN sources s ON s.id = e.source_id LEFT JOIN progress p ON p.episode_id = e.id "
    "LEFT JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid"
)


NEW_WINDOW = timedelta(days=14)    # only recent episodes can be "new" (not a freshly imported back catalogue)
NEW_THIS_WEEK = timedelta(days=7)


def annotate(row, param: str, new_since: datetime | None = None) -> dict:
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
    # Catch-up marks episodes with no Pocket Casts match played inside PodBridge. Watching it
    # again afterwards (newer progress) takes precedence.
    marked = parse_time(ep.get("marked_played_at"))
    if marked and ep["state"] != "played" and (ep["touched"] is None or ep["touched"] <= marked):
        ep["state"], ep["progress_pct"], ep["resume"], ep["resume_url"] = "played", 100.0, None, None
    # New: unwatched, first seen since your previous visit, and published recently.
    published = parse_time(ep.get("published_at"))
    first_seen = parse_time(ep.get("created_at"))
    ep["is_new"] = bool(
        new_since and ep["state"] == "unwatched" and first_seen and first_seen > new_since
        and published and datetime.now(timezone.utc) - published <= NEW_WINDOW)
    ep["thumb_key"] = f"{'yt' if youtube else 'patreon'}:{ep['patreon_post_id']}"
    ep["pc_web_url"], ep["pc_app_url"] = pocketcasts_links(
        ep.get("matched_podcast_uuid"), ep.get("pocketcasts_episode_uuid"),
        ep["resume"].position_secs if ep["resume"] else None)
    return ep


def pocketcasts_links(podcast_uuid: str | None, episode_uuid: str | None,
                      position_secs: float | None) -> tuple[str | None, str | None]:
    """(web player URL, share link) for a matched episode.

    The web player is the only way to open a specific episode on Windows (the desktop app's
    pktc:// links can't). The pca.st share link is a universal link that opens the iOS app,
    at `t` seconds if given."""
    if not (podcast_uuid and episode_uuid):
        return None, None
    web = f"https://pocketcasts.com/podcasts/{podcast_uuid}/{episode_uuid}"
    app = f"https://pca.st/episode/{episode_uuid}"
    if position_secs and position_secs >= 1:
        app += f"?t={int(position_secs)}"
    return web, app


@dataclass
class Show:
    source_id: int
    podcast_uuid: str | None
    title: str
    source_label: str
    kind: str
    episodes: list[dict] = field(default_factory=list)
    hidden_episodes: list[dict] = field(default_factory=list)

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
    def new_count(self) -> int:
        return sum(1 for e in self.episodes if e.get("is_new"))

    @property
    def latest(self) -> str:
        return max((e["published_at"] or "" for e in self.episodes), default="")


def build_shows(conn: sqlite3.Connection, param: str, new_since: datetime | None = None) -> list[Show]:
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
        ep = annotate(row, param, new_since)
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
            ep["show_scope"] = f"{show.source_id}/{show.slug}"
            (show.hidden_episodes if ep["hidden"] else show.episodes).append(ep)
    return sorted(shows.values(), key=lambda sh: sh.latest, reverse=True)


def all_episodes(shows: list[Show]) -> list[dict]:
    return [ep for show in shows for ep in show.episodes]


def continue_watching(shows: list[Show], limit: int | None = 12) -> list[dict]:
    eps = [e for e in all_episodes(shows) if e["state"] == "in_progress" and e["resume_url"]]
    eps.sort(key=lambda e: e["touched"].timestamp() if e["touched"] else 0, reverse=True)
    return eps[:limit] if limit else eps


def up_next(shows: list[Show], limit: int = 12) -> list[dict]:
    """Like Plex's On Deck: for each show you've played or started something in, the first
    unwatched episode published after the newest one you've touched. Shows you were active in
    most recently come first."""
    picks: list[tuple[float, dict]] = []
    for show in shows:
        dated = sorted((e for e in show.episodes if e["published_at"]), key=lambda e: _ts(e["published_at"]))
        touched = [i for i, e in enumerate(dated) if e["state"] in ("played", "in_progress")]
        if not touched:
            continue
        following = next((e for e in dated[touched[-1] + 1:] if e["state"] == "unwatched"), None)
        if following is None:
            continue
        last_active = max((e["touched"].timestamp() for e in show.episodes if e["touched"]), default=0.0)
        picks.append((last_active, following))
    picks.sort(key=lambda pick: pick[0], reverse=True)
    return [episode for _, episode in picks[:limit]]


def new_this_week(shows: list[Show], limit: int = 16) -> list[dict]:
    """Unwatched episodes published in the last 7 days, newest first."""
    cutoff = datetime.now(timezone.utc) - NEW_THIS_WEEK
    eps = [e for e in all_episodes(shows)
           if e["state"] == "unwatched" and (p := parse_time(e["published_at"])) and p >= cutoff]
    eps.sort(key=lambda e: _ts(e["published_at"]), reverse=True)
    return eps[:limit]


def search(shows: list[Show], query: str, limit: int = 100) -> tuple[list[Show], list[dict]]:
    """Shows and episodes whose titles contain every word of the query (accent, case and
    apostrophe insensitive), newest episodes first."""
    words = fold(query).split()
    if not words:
        return [], []
    matching_shows = [s for s in shows if all(w in fold(s.title) for w in words)]
    hits = [e for e in all_episodes(shows)
            if all(w in fold(f"{e['title']} {e.get('show_title', '')}") for w in words)]
    hits.sort(key=lambda e: _ts(e["published_at"] or ""), reverse=True)
    return matching_shows, hits[:limit]


def fold(text: str) -> str:
    """Lower-case, strip accents and quote marks, normalise dashes: 'Pierre’s' -> 'pierres'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[‘’“”'\"]", "", text).replace("–", "-").replace("—", "-")
    return text.casefold()


def recently_added(shows: list[Show], limit: int = 16) -> list[dict]:
    eps = [e for e in all_episodes(shows) if e["published_at"]]
    eps.sort(key=lambda e: _ts(e["published_at"]), reverse=True)
    return eps[:limit]


def _ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
