"""Catch-up: mark an episode and everything older in its show as played, in one go, with undo.

Matched episodes are marked played in Pocket Casts (one write each, so a big backlog runs in
the background). Episodes with no Pocket Casts match are marked played inside PodBridge only.
Each episode's previous state is recorded first, so Undo can put everything back.
Like Mark played, this is an explicit choice, so dry run doesn't apply.
"""

from __future__ import annotations

import logging
import sqlite3

from .db import utcnow
from .http import TransportError
from .pocketcasts import STATUS_PLAYED, STATUS_UNPLAYED, PocketCastsClient, PocketCastsError
from .sync import _lock

log = logging.getLogger(__name__)


def create_batch(conn: sqlite3.Connection, episode_ids: list[int], label: str, scope: str) -> int:
    with conn:
        batch_id = conn.execute(
            "INSERT INTO catch_up_batches (created_at, label, scope, total) VALUES (?, ?, ?, ?)",
            (utcnow(), label, scope, len(episode_ids))).lastrowid
        for episode_id in episode_ids:
            conn.execute(
                "INSERT INTO catch_up_items (batch_id, episode_id, prev_pc_status, prev_pc_position, "
                "prev_marked_played_at) SELECT ?, e.id, p.pocketcasts_status, p.pocketcasts_position_secs, "
                "e.marked_played_at FROM episodes e LEFT JOIN progress p ON p.episode_id = e.id WHERE e.id = ?",
                (batch_id, episode_id))
    return batch_id


def _items(conn, batch_id: int, done: int):
    return conn.execute(
        "SELECT i.*, e.pocketcasts_episode_uuid, e.duration_secs, pe.podcast_uuid, pe.duration_secs AS pc_duration "
        "FROM catch_up_items i JOIN episodes e ON e.id = i.episode_id "
        "LEFT JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid "
        "WHERE i.batch_id = ? AND i.done = ? ORDER BY i.episode_id", (batch_id, done)).fetchall()


def _write_pc(conn, pocketcasts: PocketCastsClient, item, status: int, position: float) -> None:
    duration = int(item["pc_duration"] or item["duration_secs"] or 0)
    pocketcasts.update_episode(item["pocketcasts_episode_uuid"], item["podcast_uuid"],
                               position=int(position), duration=duration, status=status)
    # Updated directly (not via apply_pocketcasts_state) so a bulk catch-up doesn't flood History.
    conn.execute("UPDATE progress SET pocketcasts_status = ?, pocketcasts_position_secs = ? WHERE episode_id = ?",
                 (status, position, item["episode_id"]))
    conn.execute("UPDATE pocketcasts_episodes SET playing_status = ?, played_up_to = ? WHERE uuid = ?",
                 (status, position, item["pocketcasts_episode_uuid"]))


def _matched(item) -> bool:
    return bool(item["pocketcasts_episode_uuid"] and item["podcast_uuid"])


def run_batch(conn: sqlite3.Connection, batch_id: int, pocketcasts: PocketCastsClient | None) -> None:
    """Mark every pending item played. Resumable: items already done are skipped."""
    with _lock:  # not alongside a sync touching the same episodes
        with conn:
            conn.execute("UPDATE catch_up_batches SET status = 'running', error = NULL WHERE id = ?", (batch_id,))
        try:
            for item in _items(conn, batch_id, done=0):
                with conn:
                    if _matched(item) and pocketcasts is not None:
                        if item["prev_pc_status"] != STATUS_PLAYED:
                            duration = item["pc_duration"] or item["duration_secs"] or 0
                            _write_pc(conn, pocketcasts, item, STATUS_PLAYED, float(int(duration)))
                    else:
                        conn.execute("UPDATE episodes SET marked_played_at = ? WHERE id = ?",
                                     (utcnow(), item["episode_id"]))
                    conn.execute("UPDATE catch_up_items SET done = 1 WHERE batch_id = ? AND episode_id = ?",
                                 (batch_id, item["episode_id"]))
                    conn.execute("UPDATE catch_up_batches SET processed = processed + 1 WHERE id = ?", (batch_id,))
            final, error = "done", None
        except (PocketCastsError, TransportError) as exc:
            final, error = "failed", f"{type(exc).__name__}: {exc}"
            log.warning("Catch-up batch %s stopped: %s", batch_id, error)
        with conn:
            conn.execute("UPDATE catch_up_batches SET status = ?, error = ? WHERE id = ?", (final, error, batch_id))


def undo_batch(conn: sqlite3.Connection, batch_id: int, pocketcasts: PocketCastsClient | None) -> None:
    """Put every processed item back the way it was before the batch."""
    with _lock:
        with conn:
            conn.execute("UPDATE catch_up_batches SET status = 'undoing', error = NULL WHERE id = ?", (batch_id,))
        try:
            for item in _items(conn, batch_id, done=1):
                with conn:
                    if _matched(item) and pocketcasts is not None:
                        if item["prev_pc_status"] != STATUS_PLAYED:
                            _write_pc(conn, pocketcasts, item, item["prev_pc_status"] or STATUS_UNPLAYED,
                                      item["prev_pc_position"] or 0.0)
                    conn.execute("UPDATE episodes SET marked_played_at = ? WHERE id = ?",
                                 (item["prev_marked_played_at"], item["episode_id"]))
                    conn.execute("UPDATE catch_up_items SET done = 0 WHERE batch_id = ? AND episode_id = ?",
                                 (batch_id, item["episode_id"]))
                    conn.execute("UPDATE catch_up_batches SET processed = processed - 1 WHERE id = ?", (batch_id,))
            final, error = "undone", None
        except (PocketCastsError, TransportError) as exc:
            final, error = "failed", f"Undo stopped: {type(exc).__name__}: {exc}"
        with conn:
            conn.execute("UPDATE catch_up_batches SET status = ?, error = ? WHERE id = ?", (final, error, batch_id))


def latest_for_scope(conn: sqlite3.Connection, scope: str):
    """The most recent batch for a show, if it's from the last day and not undone."""
    return conn.execute(
        "SELECT * FROM catch_up_batches WHERE scope = ? AND status != 'undone' "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day') ORDER BY id DESC LIMIT 1",
        (scope,)).fetchone()
