"""The sync engine: Patreon / YouTube -> Pocket Casts, one full sweep per run.

Order per run:
  1. Per source kind (Patreon, YouTube): session check + discovery. A dead
     session skips that kind only and marks the run aborted.
  2. Pocket Casts catalogue + user state + auto-matching (fresh state every run).
  3. Per matched episode: interpret source progress, apply the rules, write
     (unless dry run) and record a sync_event.

Episodes whose source progress hasn't changed since the last decision are
counted but get no event, to keep the log readable at 96 runs a day.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .crypto import SecretError
from .db import utcnow
from .discovery import discover_all, discover_youtube_all, has_sources, note_youtube_expiry
from .http import TransportError
from .linking import refresh_all
from .patreon import PatreonClient, PatreonError, PatreonSessionExpired
from .pocketcasts import PocketCastsClient, PocketCastsError
from .rules import decide, interpret_patreon
from .settings_store import SettingsStore
from .youtube import YouTubeClient, YouTubeError, YouTubeSessionExpired

log = logging.getLogger(__name__)

_lock = threading.Lock()


class SyncBusy(RuntimeError):
    pass


@dataclass
class RunSummary:
    run_id: int
    status: str
    dry_run: bool
    checked: int = 0
    updated: int = 0
    error: str | None = None
    actions: dict[str, int] = field(default_factory=dict)


def _hms(secs: float | None) -> str:
    if secs is None:
        return "–"
    total = int(secs)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def run_sync(conn: sqlite3.Connection, store: SettingsStore, patreon: PatreonClient | None,
             pocketcasts: PocketCastsClient, tier: str, youtube: YouTubeClient | None = None,
             unavailable: dict[str, str] | None = None, art_dir: Path | None = None,
             youtube_detail_budget: int | None = None) -> RunSummary:
    """`unavailable` maps a source kind to why its client couldn't be built (e.g. not configured).
    `art_dir`, if given, is where Patreon thumbnails are cached during discovery.
    `youtube_detail_budget` caps YouTube watch-page lookups (None = the interactive default)."""
    if not _lock.acquire(blocking=False):
        raise SyncBusy("A sync is already running")
    try:
        return _run(conn, store, patreon, youtube, pocketcasts, tier, unavailable or {}, art_dir,
                    youtube_detail_budget)
    finally:
        _lock.release()


def _discover(conn, store, kind: str, client, unavailable: dict[str, str],
              art_dir: Path | None = None, youtube_detail_budget: int | None = None) -> tuple[str, str] | None:
    """Discovery for one source kind. Returns (status, message) on failure, None on success or no sources.
    Each kind fails independently: a dead YouTube login doesn't stop Patreon, and vice versa."""
    if not has_sources(conn, kind):
        return None
    label = {"patreon": "Patreon", "youtube": "YouTube"}[kind]
    if client is None:
        return "error", f"{label}: {unavailable.get(kind, 'not configured')}"
    try:
        if kind == "patreon":
            discover_all(conn, store, client, art_dir)
        else:
            discover_youtube_all(conn, store, client, youtube_detail_budget)
    except (PatreonSessionExpired, YouTubeSessionExpired) as exc:
        store.set(f"{kind}_status", "expired")
        store.set(f"{kind}_verified_at", utcnow())
        if kind == "youtube":
            note_youtube_expiry(store)
        return "aborted", f"{label} session expired: {exc}"
    except (PatreonError, YouTubeError, TransportError, SecretError) as exc:
        return "error", f"{label}: {type(exc).__name__}: {exc}"
    return None


def _run(conn, store, patreon, youtube, pocketcasts, tier, unavailable, art_dir,
         youtube_detail_budget) -> RunSummary:
    dry_run = store.get_bool("dry_run")
    with conn:
        run_id = conn.execute(
            "INSERT INTO sync_runs (started_at, tier, dry_run, status) VALUES (?, ?, ?, 'running')",
            (utcnow(), tier, int(dry_run)),
        ).lastrowid
    summary = RunSummary(run_id=run_id, status="running", dry_run=dry_run)
    failures: list[tuple[str, str]] = []
    try:
        for kind, client in (("patreon", patreon), ("youtube", youtube)):
            failure = _discover(conn, store, kind, client, unavailable, art_dir, youtube_detail_budget)
            if failure:
                failures.append(failure)
        # Episodes whose source failed this run keep their old progress, which the
        # "unchanged since last decision" rule skips, so processing is still safe.
        refresh_all(conn, store, pocketcasts)
        _process(conn, run_id, pocketcasts, dry_run, summary)
    except (PocketCastsError, TransportError, SecretError) as exc:
        failures.append(("error", f"Pocket Casts: {type(exc).__name__}: {exc}"))
    except Exception as exc:  # noqa: BLE001 - record and keep the scheduler alive
        log.exception("Sync run %s crashed", run_id)
        failures.append(("error", f"Unexpected {type(exc).__name__}"))
    if not failures:
        summary.status = "ok"
    else:
        summary.status = "aborted" if all(s == "aborted" for s, _ in failures) else "error"
        summary.error = " · ".join(message for _, message in failures)
    with conn:
        conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, episodes_checked = ?, episodes_updated = ?, "
            "error = ? WHERE id = ?",
            (utcnow(), summary.status, summary.checked, summary.updated, summary.error, run_id),
        )
    log.info("Sync run %s (%s%s): %s, %s checked, %s updated", run_id, tier, ", dry run" if dry_run else "",
             summary.status, summary.checked, summary.updated)
    return summary


def _event(conn, run_id: int, episode_id: int, action: str, detail: str, summary: RunSummary) -> None:
    conn.execute("INSERT INTO sync_events (run_id, episode_id, action, detail) VALUES (?, ?, ?, ?)",
                 (run_id, episode_id, action, detail))
    summary.actions[action] = summary.actions.get(action, 0) + 1


def _mark_decided(conn, episode_id: int, patreon_updated_at: str) -> None:
    conn.execute("UPDATE progress SET synced_patreon_updated_at = ?, last_synced_at = ? WHERE episode_id = ?",
                 (patreon_updated_at, utcnow(), episode_id))


def _process(conn, run_id: int, pocketcasts: PocketCastsClient, dry_run: bool, summary: RunSummary) -> None:
    rows = conn.execute(
        "SELECT e.id, e.title, e.duration_secs, e.pocketcasts_episode_uuid, s.kind, "
        "pe.podcast_uuid AS pocketcasts_podcast_uuid, "  # the matched episode's own podcast
        "pe.duration_secs AS pc_duration, p.patreon_position_secs, p.patreon_is_watched, p.patreon_updated_at, "
        "p.pocketcasts_status, p.pocketcasts_position_secs, p.synced_patreon_updated_at "
        "FROM episodes e JOIN sources s ON s.id = e.source_id "
        "JOIN progress p ON p.episode_id = e.id "
        "LEFT JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid "
        "WHERE s.enabled = 1 AND p.patreon_updated_at IS NOT NULL ORDER BY e.published_at DESC"
    ).fetchall()
    prefix = "Dry run: would " if dry_run else ""

    for row in rows:
        summary.checked += 1
        if row["patreon_updated_at"] == row["synced_patreon_updated_at"]:
            continue  # unchanged since last decision (spec rule 2)
        episode_id = row["id"]
        src = "YouTube" if row["kind"] == "youtube" else "Patreon"
        if not row["pocketcasts_episode_uuid"] or not row["pocketcasts_podcast_uuid"]:
            with conn:
                _event(conn, run_id, episode_id, "no_match",
                       f"{src} at {_hms(row['patreon_position_secs'])} but no Pocket Casts match", summary)
                _mark_decided(conn, episode_id, row["patreon_updated_at"])
            continue

        # "Near the end" is judged on the source's own length; writes use the Pocket Casts length
        # (a YouTube video and the podcast audio can differ in length).
        duration = row["pc_duration"] or row["duration_secs"]
        playback = interpret_patreon(row["patreon_position_secs"], bool(row["patreon_is_watched"]),
                                     row["duration_secs"] or duration)
        decision = decide(playback, row["pocketcasts_status"], row["pocketcasts_position_secs"], duration)
        if decision is None:
            with conn:
                _mark_decided(conn, episode_id, row["patreon_updated_at"])
            continue

        pc_pos = _hms(row["pocketcasts_position_secs"])
        if decision.action == "skipped_played":
            detail = "Already played in Pocket Casts; left alone"
        elif decision.action == "skipped_behind":
            detail = f"{src} {_hms(playback.position_secs)} isn't ahead of Pocket Casts {pc_pos} by 15 s+"
        elif decision.action == "mark_played":
            detail = (f"{prefix}mark played ({src} at {_hms(playback.position_secs)} "
                      f"of {_hms(row['duration_secs'] or duration)})")
        else:
            detail = f"{prefix}set position {pc_pos} → {_hms(decision.position)}"

        writes = decision.action in ("set_position", "mark_played")
        if writes and not dry_run:
            pocketcasts.update_episode(row["pocketcasts_episode_uuid"], row["pocketcasts_podcast_uuid"],
                                       position=decision.position, duration=int(duration or decision.position),
                                       status=decision.status)
            summary.updated += 1
        with conn:
            _event(conn, run_id, episode_id, decision.action, detail, summary)
            if writes and not dry_run:
                conn.execute("UPDATE progress SET pocketcasts_status = ?, pocketcasts_position_secs = ? "
                             "WHERE episode_id = ?", (decision.status, decision.position, episode_id))
                conn.execute("UPDATE pocketcasts_episodes SET playing_status = ?, played_up_to = ? WHERE uuid = ?",
                             (decision.status, decision.position, row["pocketcasts_episode_uuid"]))
            if not (writes and dry_run):
                # A dry-run "would write" stays pending so a real run still acts on it.
                _mark_decided(conn, episode_id, row["patreon_updated_at"])
