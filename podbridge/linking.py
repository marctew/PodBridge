"""Pocket Casts side of discovery: catalogue import, auto-matching, manual
matching, and the Pocket Casts columns of `progress` for matched episodes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .db import utcnow
from .matching import PatreonSide, PocketSide, match_episodes
from .pocketcasts import STATUS_UNPLAYED, EpisodeState, PocketCastsClient
from .settings_store import SettingsStore

log = logging.getLogger(__name__)


class MatchError(ValueError):
    pass


@dataclass
class LinkResult:
    source_id: int
    label: str
    catalogue_size: int = 0
    newly_matched: int = 0
    matched_total: int = 0
    unmatched: int = 0
    catalogue_truncated: bool = False
    by_method: dict[str, int] = field(default_factory=dict)


def check_pocketcasts_login(store: SettingsStore, client: PocketCastsClient) -> bool:
    ok = client.check_login()
    store.set("pocketcasts_status", "ok" if ok else "error")
    store.set("pocketcasts_verified_at", utcnow())
    return ok


def store_catalogue(conn: sqlite3.Connection, podcast_uuid: str, catalogue, states: dict[str, EpisodeState]) -> None:
    now = utcnow()
    for ep in catalogue:
        state = states.get(ep.uuid)
        conn.execute(
            "INSERT INTO pocketcasts_episodes (uuid, podcast_uuid, title, published_at, duration_secs, "
            "playing_status, played_up_to, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (uuid) DO UPDATE SET podcast_uuid = excluded.podcast_uuid, title = excluded.title, "
            "published_at = excluded.published_at, duration_secs = excluded.duration_secs, "
            "playing_status = excluded.playing_status, played_up_to = excluded.played_up_to, "
            "fetched_at = excluded.fetched_at",
            (ep.uuid, podcast_uuid, ep.title, ep.published_at, ep.duration_secs,
             state.status if state else STATUS_UNPLAYED, state.played_up_to if state else 0.0, now),
        )


def reset_sync_marker(conn: sqlite3.Connection, episode_id: int) -> None:
    """A match changed, so the next run must re-evaluate this episode even if Patreon hasn't changed."""
    conn.execute("UPDATE progress SET synced_patreon_updated_at = NULL WHERE episode_id = ?", (episode_id,))


def apply_pocketcasts_state(conn: sqlite3.Connection, episode_id: int, status: int | None,
                            position: float | None) -> None:
    conn.execute(
        "INSERT INTO progress (episode_id, pocketcasts_status, pocketcasts_position_secs) VALUES (?, ?, ?) "
        "ON CONFLICT (episode_id) DO UPDATE SET pocketcasts_status = excluded.pocketcasts_status, "
        "pocketcasts_position_secs = excluded.pocketcasts_position_secs",
        (episode_id, status, position),
    )


def sync_matched_states(conn: sqlite3.Connection, source_id: int) -> None:
    """Copy cached Pocket Casts state onto every matched episode of a source."""
    rows = conn.execute(
        "SELECT e.id, pe.playing_status, pe.played_up_to FROM episodes e "
        "JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid WHERE e.source_id = ?",
        (source_id,),
    ).fetchall()
    for row in rows:
        apply_pocketcasts_state(conn, row["id"], row["playing_status"], row["played_up_to"])


def auto_match_source(conn: sqlite3.Connection, source: sqlite3.Row) -> int:
    podcast_uuid = source["pocketcasts_podcast_uuid"]
    pending = [
        PatreonSide(r["id"], r["title"], r["published_at"], r["duration_secs"])
        for r in conn.execute(
            "SELECT id, title, published_at, duration_secs FROM episodes "
            "WHERE source_id = ? AND match_method = 'none' AND match_locked = 0", (source["id"],))
    ]
    pocket = [
        PocketSide(r["uuid"], r["title"], r["published_at"], r["duration_secs"])
        for r in conn.execute(
            "SELECT uuid, title, published_at, duration_secs FROM pocketcasts_episodes WHERE podcast_uuid = ?",
            (podcast_uuid,))
    ]
    taken = {r[0] for r in conn.execute(
        "SELECT pocketcasts_episode_uuid FROM episodes WHERE pocketcasts_episode_uuid IS NOT NULL")}
    matches = match_episodes(pending, pocket, taken)
    for episode_id, (uuid, method) in matches.items():
        conn.execute(
            "UPDATE episodes SET pocketcasts_episode_uuid = ?, match_method = ?, updated_at = ? WHERE id = ?",
            (uuid, method, utcnow(), episode_id),
        )
        reset_sync_marker(conn, episode_id)
    return len(matches)


def refresh_source(conn: sqlite3.Connection, client: PocketCastsClient, source: sqlite3.Row) -> LinkResult:
    result = LinkResult(source_id=source["id"], label=source["label"])
    podcast_uuid = source["pocketcasts_podcast_uuid"]
    catalogue, truncated = client.list_episodes(podcast_uuid)
    states = client.episode_states(podcast_uuid)
    with conn:
        store_catalogue(conn, podcast_uuid, catalogue, states)
        result.newly_matched = auto_match_source(conn, source)
        sync_matched_states(conn, source["id"])
    result.catalogue_size = len(catalogue)
    result.catalogue_truncated = truncated
    for row in conn.execute(
        "SELECT match_method, COUNT(*) AS n FROM episodes WHERE source_id = ? GROUP BY match_method",
        (source["id"],),
    ):
        result.by_method[row["match_method"]] = row["n"]
    result.unmatched = result.by_method.get("none", 0)
    result.matched_total = sum(n for m, n in result.by_method.items() if m != "none")
    if truncated:
        log.warning("Pocket Casts catalogue for source %s reports more episodes than returned", source["id"])
    return result


def refresh_all(conn: sqlite3.Connection, store: SettingsStore, client: PocketCastsClient) -> list[LinkResult]:
    check_pocketcasts_login(store, client)
    sources = conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND pocketcasts_podcast_uuid IS NOT NULL ORDER BY id"
    ).fetchall()
    return [refresh_source(conn, client, source) for source in sources]


def link_source(conn: sqlite3.Connection, source_id: int, podcast_uuid: str) -> None:
    """Point a source at a Pocket Casts podcast. Changing it discards that source's matches."""
    with conn:
        current = conn.execute("SELECT pocketcasts_podcast_uuid FROM sources WHERE id = ?", (source_id,)).fetchone()
        if current is None:
            raise MatchError("No such source")
        if current[0] == podcast_uuid:
            return
        conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = ? WHERE id = ?", (podcast_uuid, source_id))
        conn.execute(
            "UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none', match_locked = 0 "
            "WHERE source_id = ?", (source_id,))
        conn.execute(
            "UPDATE progress SET pocketcasts_status = NULL, pocketcasts_position_secs = NULL, "
            "synced_patreon_updated_at = NULL "
            "WHERE episode_id IN (SELECT id FROM episodes WHERE source_id = ?)", (source_id,))


def set_manual_match(conn: sqlite3.Connection, episode_id: int, uuid: str) -> None:
    with conn:
        row = conn.execute(
            "SELECT e.id, s.pocketcasts_podcast_uuid FROM episodes e JOIN sources s ON s.id = e.source_id "
            "WHERE e.id = ?", (episode_id,)).fetchone()
        if row is None:
            raise MatchError("No such episode")
        candidate = conn.execute("SELECT * FROM pocketcasts_episodes WHERE uuid = ? AND podcast_uuid = ?",
                                 (uuid, row["pocketcasts_podcast_uuid"])).fetchone()
        if candidate is None:
            raise MatchError("That Pocket Casts episode isn't in this source's podcast")
        other = conn.execute("SELECT title FROM episodes WHERE pocketcasts_episode_uuid = ? AND id != ?",
                             (uuid, episode_id)).fetchone()
        if other is not None:
            raise MatchError(f"Already matched to “{other['title']}”. Unlink that one first.")
        conn.execute(
            "UPDATE episodes SET pocketcasts_episode_uuid = ?, match_method = 'manual', match_locked = 0, "
            "updated_at = ? WHERE id = ?", (uuid, utcnow(), episode_id))
        apply_pocketcasts_state(conn, episode_id, candidate["playing_status"], candidate["played_up_to"])
        reset_sync_marker(conn, episode_id)


def unlink(conn: sqlite3.Connection, episode_id: int) -> None:
    """Remove a match and stop auto-matching from re-linking this episode."""
    with conn:
        conn.execute(
            "UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none', match_locked = 1, "
            "updated_at = ? WHERE id = ?", (utcnow(), episode_id))
        apply_pocketcasts_state(conn, episode_id, None, None)
        reset_sync_marker(conn, episode_id)


def allow_auto_match(conn: sqlite3.Connection, episode_id: int) -> None:
    with conn:
        conn.execute("UPDATE episodes SET match_locked = 0 WHERE id = ? AND match_method = 'none'", (episode_id,))
