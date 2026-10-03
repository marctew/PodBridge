"""Sync engine (spec section 7 per-episode rules), with fake clients and no network."""

from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_linking import fake_pocketcasts

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.linking import link_source
from podbridge.patreon import Progress
from podbridge.pocketcasts import EpisodeState
from podbridge.rules import Decision, PatreonPlayback, decide
from podbridge.settings_store import SettingsStore
from podbridge.sync import run_sync

PEEP = "171048709"  # Patreon 381.98 s (is_watching); Pocket Casts in progress at 1200 s
DAD = "170000002"   # Patreon watched to the end; Pocket Casts already played
AUDIO = "170000003"  # never played on either side


def posts_with(**progress_by_post: Progress):
    return [replace(p, progress=progress_by_post.get(p.post_id, p.progress)) for p in fixture_posts()]


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    link_source(conn, 1, "pc-podcast-bb")
    return conn, store


def events(conn, run_id):
    return [(r["patreon_post_id"], r["action"]) for r in conn.execute(
        "SELECT e.patreon_post_id, ev.action FROM sync_events ev JOIN episodes e ON e.id = ev.episode_id "
        "WHERE ev.run_id = ? ORDER BY ev.id", (run_id,))]


def test_decide_rules():
    assert decide(PatreonPlayback(False, 600.0), 2, 100.0, 2838) == Decision("set_position", 600, 2)
    assert decide(PatreonPlayback(False, 110.0), 2, 100.0, 2838) == Decision("skipped_behind")  # within 15 s
    assert decide(PatreonPlayback(False, 50.0), 2, 1200.0, 2838) == Decision("skipped_behind")   # never rewind
    assert decide(PatreonPlayback(True, 2800.0), 2, 100.0, 2838.4) == Decision("mark_played", 2838, 3)
    assert decide(PatreonPlayback(True, 2800.0), 3, 2838.0, 2838) == Decision("skipped_played")
    assert decide(PatreonPlayback(False, 600.0), 3, 2838.0, 2838) == Decision("skipped_played")  # played wins
    assert decide(PatreonPlayback(False, None), 1, 0.0, 2838) is None


def test_never_rewinds_and_respects_played(env):
    conn, store = env
    store.set("dry_run", "0")
    pc = fake_pocketcasts()
    summary = run_sync(conn, store, FakePatreonClient(fixture_posts()), pc, "manual")
    assert summary.status == "ok"
    assert pc.updates == []  # Patreon 6:21 is behind Pocket Casts 20:00; DAD already played in PC
    assert sorted(events(conn, summary.run_id)) == [(DAD, "skipped_played"), (PEEP, "skipped_behind")]


def test_writes_position_when_patreon_is_ahead(env):
    conn, store = env
    store.set("dry_run", "0")
    pc = fake_pocketcasts()
    posts = posts_with(**{PEEP: Progress(2444.5, True, "is_watched", "2026-10-03T13:00:00+00:00")})
    summary = run_sync(conn, store, FakePatreonClient(posts), pc, "manual")
    # Early "watched" at 40:44 of 47:18 syncs as a position, not played.
    assert pc.updates == [("pc-ep-peep", "pc-podcast-bb", 2444, 2838, 2)]
    assert summary.updated == 1
    row = conn.execute("SELECT pocketcasts_status, pocketcasts_position_secs FROM progress p "
                       "JOIN episodes e ON e.id = p.episode_id WHERE e.patreon_post_id = ?", (PEEP,)).fetchone()
    assert tuple(row) == (2, 2444)


def test_marks_played_near_the_end(env):
    conn, store = env
    store.set("dry_run", "0")
    pc = fake_pocketcasts()
    posts = posts_with(**{PEEP: Progress(2800.0, True, "is_watched", "2026-10-03T13:00:00+00:00")})
    run_sync(conn, store, FakePatreonClient(posts), pc, "manual")
    assert pc.updates == [("pc-ep-peep", "pc-podcast-bb", 2838, 2838, 3)]


def test_unchanged_progress_is_skipped_next_run(env):
    conn, store = env
    store.set("dry_run", "0")
    posts = posts_with(**{PEEP: Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00")})
    run_sync(conn, store, FakePatreonClient(posts), fake_pocketcasts(), "manual")
    pc = fake_pocketcasts()
    second = run_sync(conn, store, FakePatreonClient(posts), pc, "full")
    assert pc.updates == []
    assert events(conn, second.run_id) == []
    assert second.checked == 2  # still counted


def test_dry_run_logs_but_does_not_write_and_stays_pending(env):
    conn, store = env  # dry run is the default
    posts = posts_with(**{PEEP: Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00")})
    pc = fake_pocketcasts()
    first = run_sync(conn, store, FakePatreonClient(posts), pc, "manual")
    assert first.dry_run and pc.updates == [] and first.updated == 0
    detail = conn.execute("SELECT detail FROM sync_events WHERE run_id = ? AND action = 'set_position'",
                          (first.run_id,)).fetchone()[0]
    assert detail.startswith("Dry run: would set position 20:00 → 40:44")

    store.set("dry_run", "0")
    pc = fake_pocketcasts()
    run_sync(conn, store, FakePatreonClient(posts), pc, "manual")
    assert pc.updates == [("pc-ep-peep", "pc-podcast-bb", 2444, 2838, 2)]


def test_dead_patreon_session_aborts_without_writing(env):
    conn, store = env
    store.set("dry_run", "0")
    pc = fake_pocketcasts()
    summary = run_sync(conn, store, FakePatreonClient(fixture_posts(), logged_in=False), pc, "full")
    assert summary.status == "aborted"
    assert pc.updates == []
    run = conn.execute("SELECT status, error FROM sync_runs WHERE id = ?", (summary.run_id,)).fetchone()
    assert run["status"] == "aborted" and "expired" in run["error"]
    assert store.get("patreon_status") == "expired"


def test_pocketcasts_failure_marks_run_error(env):
    from podbridge.pocketcasts import PocketCastsBlocked
    conn, store = env
    summary = run_sync(conn, store, FakePatreonClient(fixture_posts()),
                       fake_pocketcasts(error=PocketCastsBlocked("HTTP 429")), "full")
    assert summary.status == "error" and "429" in summary.error


def test_unmatched_progress_is_logged_once(env):
    conn, store = env
    with conn:
        conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = NULL")
    first = run_sync(conn, store, FakePatreonClient(fixture_posts()), fake_pocketcasts(), "full")
    assert sorted(events(conn, first.run_id)) == [(DAD, "no_match"), (PEEP, "no_match")]
    second = run_sync(conn, store, FakePatreonClient(fixture_posts()), fake_pocketcasts(), "full")
    assert events(conn, second.run_id) == []


def test_newly_matched_episode_is_reevaluated(env):
    conn, store = env
    store.set("dry_run", "0")
    states = {"pc-ep-peep": EpisodeState("pc-ep-peep", 1, 0.0, 2838.0)}
    posts = posts_with(**{PEEP: Progress(600.0, False, "is_watching", "2026-10-03T13:00:00+00:00")})
    with conn:
        conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = NULL")
    run_sync(conn, store, FakePatreonClient(posts), fake_pocketcasts(), "full")  # logs no_match
    link_source(conn, 1, "pc-podcast-bb")
    pc = fake_pocketcasts()
    pc.states = states
    run_sync(conn, store, FakePatreonClient(posts), pc, "full")
    assert ("pc-ep-peep", "pc-podcast-bb", 600, 2838, 2) in pc.updates
