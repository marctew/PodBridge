"""Pocket Casts side of discovery: catalogue import, auto-matching, manual
matching, and the Pocket Casts columns of `progress` for matched episodes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .db import utcnow
from .matching import (
    LOOSE_DATE_TOLERANCE, PatreonSide, PocketSide, match_episodes, match_episodes_loose, parse_time,
)
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
    """Store Pocket Casts state; stamp pocketcasts_changed_at when an already-known state changes
    (a first sighting has no meaningful time, so it isn't stamped)."""
    previous = conn.execute("SELECT pocketcasts_status, pocketcasts_position_secs FROM progress "
                            "WHERE episode_id = ?", (episode_id,)).fetchone()
    changed = (previous is not None and previous[0] is not None and status is not None
               and (previous[0], previous[1]) != (status, position))
    conn.execute(
        "INSERT INTO progress (episode_id, pocketcasts_status, pocketcasts_position_secs) VALUES (?, ?, ?) "
        "ON CONFLICT (episode_id) DO UPDATE SET pocketcasts_status = excluded.pocketcasts_status, "
        "pocketcasts_position_secs = excluded.pocketcasts_position_secs",
        (episode_id, status, position),
    )
    if changed:
        conn.execute("UPDATE progress SET pocketcasts_changed_at = ? WHERE episode_id = ?", (utcnow(), episode_id))


def sync_matched_states(conn: sqlite3.Connection, source_id: int) -> None:
    """Copy cached Pocket Casts state onto every matched episode of a source."""
    rows = conn.execute(
        "SELECT e.id, pe.playing_status, pe.played_up_to FROM episodes e "
        "JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid WHERE e.source_id = ?",
        (source_id,),
    ).fetchall()
    for row in rows:
        apply_pocketcasts_state(conn, row["id"], row["playing_status"], row["played_up_to"])


def source_podcasts(conn: sqlite3.Connection, source_id: int) -> dict[str, str | None]:
    """{podcast uuid: title} for every Pocket Casts podcast a source feeds."""
    return {r["podcast_uuid"]: r["title"] for r in conn.execute(
        "SELECT podcast_uuid, title FROM source_podcasts WHERE source_id = ? ORDER BY rowid", (source_id,))}


def _clear_match(conn: sqlite3.Connection, episode_id: int, lock: bool) -> None:
    conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none', match_locked = ?, "
                 "updated_at = ? WHERE id = ?", (int(lock), utcnow(), episode_id))
    apply_pocketcasts_state(conn, episode_id, None, None)
    reset_sync_marker(conn, episode_id)


def revalidate_fuzzy_matches(conn: sqlite3.Connection, source_id: int) -> int:
    """Drop Fuzzy matches that a now-known publish date contradicts (more than the loose date
    window apart). They were made from title + length alone, before the date was fetched.
    Manual and exact-title matches are never touched. Returns how many were dropped."""
    rows = conn.execute(
        "SELECT e.id, e.published_at, pe.published_at AS pc_published FROM episodes e "
        "JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid "
        "WHERE e.source_id = ? AND e.match_method = 'auto_date' AND e.published_at IS NOT NULL",
        (source_id,)).fetchall()
    dropped = 0
    for row in rows:
        a, b = parse_time(row["published_at"]), parse_time(row["pc_published"])
        if a and b and abs(a - b) > LOOSE_DATE_TOLERANCE:
            _clear_match(conn, row["id"], lock=False)
            dropped += 1
    return dropped


def auto_match_source(conn: sqlite3.Connection, source: sqlite3.Row) -> int:
    if source["kind"] == "youtube":
        revalidate_fuzzy_matches(conn, source["id"])
    podcasts = source_podcasts(conn, source["id"])
    pending = [
        PatreonSide(r["id"], r["title"], r["published_at"], r["duration_secs"])
        for r in conn.execute(
            "SELECT id, title, published_at, duration_secs FROM episodes "
            "WHERE source_id = ? AND match_method = 'none' AND match_locked = 0", (source["id"],))
    ]
    pocket = [
        PocketSide(r["uuid"], r["title"], r["published_at"], r["duration_secs"], r["podcast_uuid"])
        for r in conn.execute(
            f"SELECT uuid, title, published_at, duration_secs, podcast_uuid FROM pocketcasts_episodes "
            f"WHERE podcast_uuid IN ({','.join('?' * len(podcasts))})", list(podcasts))
    ] if podcasts else []
    # Matches held by disabled sources don't block: disabling a narrow source in favour of a
    # wider one must let the wider one take over its episodes.
    taken = {r[0] for r in conn.execute(
        "SELECT e.pocketcasts_episode_uuid FROM episodes e JOIN sources s ON s.id = e.source_id "
        "WHERE e.pocketcasts_episode_uuid IS NOT NULL AND s.enabled = 1")}
    if source["kind"] == "youtube":
        matches = match_episodes_loose(pending, pocket, taken, podcast_titles=podcasts)
    else:
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
    fetched = []
    for podcast_uuid in source_podcasts(conn, source["id"]):
        catalogue, truncated = client.list_episodes(podcast_uuid)
        fetched.append((podcast_uuid, catalogue, client.episode_states(podcast_uuid)))
        result.catalogue_size += len(catalogue)
        result.catalogue_truncated = result.catalogue_truncated or truncated
    with conn:
        for podcast_uuid, catalogue, states in fetched:
            store_catalogue(conn, podcast_uuid, catalogue, states)
        result.newly_matched = auto_match_source(conn, source)
        sync_matched_states(conn, source["id"])
    for row in conn.execute(
        "SELECT match_method, COUNT(*) AS n FROM episodes WHERE source_id = ? GROUP BY match_method",
        (source["id"],),
    ):
        result.by_method[row["match_method"]] = row["n"]
    result.unmatched = result.by_method.get("none", 0)
    result.matched_total = sum(n for m, n in result.by_method.items() if m != "none")
    if result.catalogue_truncated:
        log.warning("Pocket Casts catalogue for source %s reports more episodes than returned", source["id"])
    return result


def refresh_all(conn: sqlite3.Connection, store: SettingsStore, client: PocketCastsClient) -> list[LinkResult]:
    check_pocketcasts_login(store, client)
    sources = conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND pocketcasts_podcast_uuid IS NOT NULL ORDER BY id"
    ).fetchall()
    return [refresh_source(conn, client, source) for source in sources]


def link_source(conn: sqlite3.Connection, source_id: int,
                podcasts: str | list[tuple[str, str | None]]) -> None:
    """Set the Pocket Casts podcasts a source feeds (a single uuid, or [(uuid, title), ...]).

    Matches into podcasts that are no longer linked are discarded; matches into podcasts
    that stay linked are kept. An empty list unlinks the source."""
    if isinstance(podcasts, str):
        podcasts = [(podcasts, None)]
    keep = [uuid for uuid, _ in podcasts]
    with conn:
        if conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise MatchError("No such source")
        conn.execute("DELETE FROM source_podcasts WHERE source_id = ?", (source_id,))
        for uuid, title in podcasts:
            conn.execute("INSERT INTO source_podcasts (source_id, podcast_uuid, title) VALUES (?, ?, ?)",
                         (source_id, uuid, title))
        conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = ? WHERE id = ?",
                     (keep[0] if keep else None, source_id))
        outside = (f"(pe.podcast_uuid IS NULL OR pe.podcast_uuid NOT IN ({','.join('?' * len(keep))}))"
                   if keep else "1 = 1")  # `NOT IN ()` isn't valid SQL, and `NOT IN (NULL)` matches nothing
        dropped = [r[0] for r in conn.execute(
            "SELECT e.id FROM episodes e LEFT JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid "
            f"WHERE e.source_id = ? AND e.pocketcasts_episode_uuid IS NOT NULL AND {outside}", [source_id, *keep])]
        for episode_id in dropped:
            conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none', "
                         "match_locked = 0 WHERE id = ?", (episode_id,))
            conn.execute("UPDATE progress SET pocketcasts_status = NULL, pocketcasts_position_secs = NULL, "
                         "synced_patreon_updated_at = NULL WHERE episode_id = ?", (episode_id,))


def set_manual_match(conn: sqlite3.Connection, episode_id: int, uuid: str) -> None:
    with conn:
        row = conn.execute("SELECT id, source_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            raise MatchError("No such episode")
        candidate = conn.execute(
            "SELECT pe.* FROM pocketcasts_episodes pe JOIN source_podcasts sp ON sp.podcast_uuid = pe.podcast_uuid "
            "WHERE pe.uuid = ? AND sp.source_id = ?", (uuid, row["source_id"])).fetchone()
        if candidate is None:
            raise MatchError("That Pocket Casts episode isn't in this source's podcasts")
        other = conn.execute(
            "SELECT e.title FROM episodes e JOIN sources s ON s.id = e.source_id "
            "WHERE e.pocketcasts_episode_uuid = ? AND e.id != ? AND s.enabled = 1", (uuid, episode_id)).fetchone()
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
        _clear_match(conn, episode_id, lock=True)


def take_over_match(conn: sqlite3.Connection, episode_id: int, uuid: str) -> str:
    """Move a Pocket Casts episode from whichever episode holds it to this one (manual match).
    The previous holder becomes unmatched but free to auto-match something else.
    Returns the previous holder's title."""
    with conn:
        holder = conn.execute(
            "SELECT e.id, e.title FROM episodes e JOIN sources s ON s.id = e.source_id "
            "WHERE e.pocketcasts_episode_uuid = ? AND e.id != ? AND s.enabled = 1", (uuid, episode_id)).fetchone()
        if holder is not None:
            _clear_match(conn, holder["id"], lock=False)
    set_manual_match(conn, episode_id, uuid)
    return holder["title"] if holder else ""


def rematch_youtube_offline(conn: sqlite3.Connection) -> int:
    """Re-run matching for YouTube sources against the cached Pocket Casts catalogue
    (no network), e.g. after a date backfill. Returns how many new matches were made."""
    total = 0
    for source in conn.execute("SELECT * FROM sources WHERE enabled = 1 AND kind = 'youtube' "
                               "AND pocketcasts_podcast_uuid IS NOT NULL").fetchall():
        with conn:
            total += auto_match_source(conn, source)
            sync_matched_states(conn, source["id"])
    return total


def widen_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Switch a collection source to all posts in its campaign. Existing episodes and matches are
    kept (episodes are keyed by Patreon post ID), and the next refresh adds the rest."""
    with conn:
        row = conn.execute("SELECT campaign_id, label FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise MatchError("No such source")
        clash = conn.execute("SELECT id FROM sources WHERE campaign_id = ? AND collection_id = '' AND id != ?",
                             (row["campaign_id"], source_id)).fetchone()
        if clash:
            raise MatchError("This campaign already has an all-posts source.")
        # "Button Boys: Hidden Cache" -> "Button Boys: all posts"
        label = row["label"]
        label = f"{label.split(':', 1)[0]}: all posts" if ":" in label else f"{label} (all posts)"
        conn.execute("UPDATE sources SET collection_id = '', label = ? WHERE id = ?", (label, source_id))


def allow_auto_match(conn: sqlite3.Connection, episode_id: int) -> None:
    with conn:
        conn.execute("UPDATE episodes SET match_locked = 0 WHERE id = ? AND match_method = 'none'", (episode_id,))
