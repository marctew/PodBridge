"""Catch-up (with undo), hidden episodes, watch history, and backup/restore."""

from __future__ import annotations

import io
import sqlite3
from dataclasses import replace

import pytest
from conftest import csrf_from
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_library import setup_library
from test_linking import fake_pocketcasts
from test_web_patreon import post

from podbridge import backup, catchup, history
from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.library import build_shows
from podbridge.linking import link_source, refresh_all
from podbridge.patreon import Progress
from podbridge.pocketcasts import EpisodeState
from podbridge.settings_store import SettingsStore
from podbridge.sync import run_sync

PEEP, DAD, AUDIO = "171048709", "170000002", "170000003"


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "c.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    link_source(conn, 1, [("pc-podcast-bb", "Button Boys")])
    refresh_all(conn, store, fake_pocketcasts())
    return conn, store


def eid(conn, post_id):
    return conn.execute("SELECT id FROM episodes WHERE patreon_post_id = ?", (post_id,)).fetchone()[0]


def states(conn):
    return {e["patreon_post_id"]: e["state"] for s in build_shows(conn, "t") for e in s.episodes}


# --- catch-up ---

def test_catch_up_marks_played_and_undo_restores(env):
    conn, _ = env
    pc = fake_pocketcasts()
    ids = [eid(conn, PEEP), eid(conn, AUDIO)]  # Peep in progress (PC 20:00), Audio unwatched
    batch = catchup.create_batch(conn, ids, "test", "1/pc-podcast-bb")
    catchup.run_batch(conn, batch, pc)
    assert states(conn)[PEEP] == states(conn)[AUDIO] == "played"
    assert {u[0] for u in pc.updates} == {"pc-ep-peep", "pc-ep-audio"}
    assert all(u[4] == 3 for u in pc.updates)
    assert tuple(conn.execute("SELECT status, processed FROM catch_up_batches").fetchone()) == ("done", 2)

    pc.updates.clear()
    catchup.undo_batch(conn, batch, pc)
    assert sorted(pc.updates) == [("pc-ep-audio", "pc-podcast-bb", 0, 1802, 1),
                                  ("pc-ep-peep", "pc-podcast-bb", 1200, 2838, 2)]
    assert states(conn)[PEEP] == "in_progress" and states(conn)[AUDIO] == "unwatched"


def test_catch_up_without_pocketcasts_match_marks_locally(env):
    conn, _ = env
    with conn:
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none' "
                     "WHERE patreon_post_id = ?", (AUDIO,))
    batch = catchup.create_batch(conn, [eid(conn, AUDIO)], "test", "1/pc-podcast-bb")
    catchup.run_batch(conn, batch, fake_pocketcasts())
    assert states(conn)[AUDIO] == "played"
    catchup.undo_batch(conn, batch, fake_pocketcasts())
    assert states(conn)[AUDIO] == "unwatched"


def test_catch_up_failure_is_resumable(env):
    from podbridge.pocketcasts import PocketCastsBlocked
    conn, _ = env
    batch = catchup.create_batch(conn, [eid(conn, AUDIO)], "test", "1/pc-podcast-bb")
    catchup.run_batch(conn, batch, fake_pocketcasts(error=PocketCastsBlocked("429")))
    assert conn.execute("SELECT status FROM catch_up_batches").fetchone()[0] == "failed"
    catchup.run_batch(conn, batch, fake_pocketcasts())
    assert tuple(conn.execute("SELECT status, processed FROM catch_up_batches").fetchone()) == ("done", 1)


def test_catch_up_web_flow(app, authed):
    setup_library(app, authed)
    page = authed.get("/library/1/pc-podcast-bb")
    assert "Played up to here" in page.get_data(as_text=True)
    html = authed.post("/episodes/1/catch-up", data={"csrf_token": csrf_from(page)},
                       follow_redirects=True).get_data(as_text=True)
    assert "Marking 2 episodes played" in html            # Peep (in progress) + Audio Only (older, unwatched)
    assert "Marked “Hidden Cache - Try Not to Peep” and everything older as played" in html
    assert "↶ Undo" in html


# --- hidden episodes ---

def test_hidden_episodes_leave_lists_and_sync(env):
    conn, store = env
    with conn:
        conn.execute("UPDATE episodes SET hidden = 1 WHERE patreon_post_id = ?", (PEEP,))
    [show] = build_shows(conn, "t")
    assert PEEP not in {e["patreon_post_id"] for e in show.episodes}
    assert [e["patreon_post_id"] for e in show.hidden_episodes] == [PEEP]
    store.set("dry_run", "0")
    ahead = [replace(p, progress=Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00"))
             if p.post_id == PEEP else p for p in fixture_posts()]
    pc = fake_pocketcasts()
    run_sync(conn, store, FakePatreonClient(ahead), pc, "full")
    assert pc.updates == []  # hidden: not synced


def test_hide_web_flow(app, authed):
    setup_library(app, authed)
    page = authed.get("/library/1/pc-podcast-bb")
    authed.post("/episodes/3/hide", data={"csrf_token": csrf_from(page), "hidden": "1",
                                          "next": "/library/1/pc-podcast-bb"})
    show = authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)
    assert "Audio Only" not in show and "Hidden (1)" in show
    hidden = authed.get("/library/1/pc-podcast-bb?filter=hidden").get_data(as_text=True)
    assert "Audio Only" in hidden and ">Unhide<" in hidden
    assert "Audio Only" not in authed.get("/episodes").get_data(as_text=True)
    assert "Audio Only" in authed.get("/episodes?filter=hidden").get_data(as_text=True)


def test_hide_all_unmatched(app, authed):
    setup_library(app, authed)
    with app.app_context():
        from podbridge.db import get_db
        db = get_db()
        with db:
            db.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none' WHERE id = 3")
    page = authed.get("/library/1/pc-podcast-bb")
    assert "Hide 1 unmatched" in page.get_data(as_text=True)
    html = authed.post("/library/1/pc-podcast-bb/hide-unmatched", data={"csrf_token": csrf_from(page)},
                       follow_redirects=True).get_data(as_text=True)
    assert "Hid 1 unmatched episode." in html


# --- history ---

def test_history_records_both_sides(env):
    conn, store = env
    # Backfill/new discovery logged the Patreon positions with Patreon's own timestamps.
    sides = {r[0] for r in conn.execute("SELECT side FROM watch_history")}
    assert "patreon" in sides
    pc = fake_pocketcasts()
    pc.states["pc-ep-peep"] = EpisodeState("pc-ep-peep", 2, 1500.0, 2838.0)
    refresh_all(conn, store, pc)  # Pocket Casts moved 20:00 -> 25:00
    entry = conn.execute("SELECT side, position_secs FROM watch_history WHERE side = 'pocketcasts' "
                         "ORDER BY id DESC").fetchone()
    assert tuple(entry) == ("pocketcasts", 1500.0)
    assert history.recently_watched_ids(conn)[0] == eid(conn, PEEP)


def test_early_patreon_watched_flag_isnt_logged_as_finished(env):
    conn, store = env
    early = [replace(p, progress=Progress(2444.5, True, "is_watched", "2026-10-03T14:00:00+00:00"))
             if p.post_id == PEEP else p for p in fixture_posts()]
    discover_all(conn, store, FakePatreonClient(early))
    row = conn.execute("SELECT position_secs, played FROM watch_history WHERE episode_id = ? "
                       "ORDER BY id DESC", (eid(conn, PEEP),)).fetchone()
    assert tuple(row) == (2444.5, 0)  # 40:44 of 47:17 isn't "finished"


def test_unchanged_progress_isnt_logged_twice(env):
    conn, store = env
    before = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    refresh_all(conn, store, fake_pocketcasts())
    assert conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0] == before


def test_history_page_and_home_row(app, authed):
    setup_library(app, authed)
    page = authed.get("/history").get_data(as_text=True)
    assert "Watched on Patreon" in page and "Hidden Cache - Try Not to Peep" in page
    assert "Recently watched" in authed.get("/").get_data(as_text=True)


def test_migration_backfills_history(tmp_path):
    from podbridge.db import migrations
    conn = connect(tmp_path / "m7.db")
    for version, path in migrations():
        if version > 6:
            break
        conn.executescript(f"BEGIN;{path.read_text(encoding='utf-8')}PRAGMA user_version = {version};COMMIT;")
    with conn:
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title) VALUES (1, 'p1', 'T')")
        conn.execute("INSERT INTO progress (episode_id, patreon_position_secs, patreon_updated_at, "
                     "pocketcasts_status, pocketcasts_position_secs, pocketcasts_changed_at) "
                     "VALUES (1, 381.98, '2026-10-03T12:21:00.628+00:00', 2, 1200, '2026-10-03T13:00:00Z')")
    migrate(conn)
    rows = [tuple(r) for r in conn.execute("SELECT side, position_secs, at FROM watch_history ORDER BY at")]
    assert rows == [("patreon", 381.98, "2026-10-03T12:21:00Z"), ("pocketcasts", 1200.0, "2026-10-03T13:00:00Z")]


# --- backup / restore ---

def test_backup_round_trip_and_key_check(env, tmp_path):
    conn, store = env
    store.set_secret("patreon_session_id", "cookie-123")
    data = backup.make_backup(conn)
    assert data[:16] == backup.SQLITE_HEADER
    path = tmp_path / "b.db"
    path.write_bytes(data)
    box = store._box
    info = backup.inspect(path, box)
    assert (info.episodes, info.has_secrets, info.secrets_readable) == (3, True, True)

    other = SecretBox(Fernet.generate_key().decode())
    assert backup.inspect(path, other).secrets_readable is False
    fresh = connect(tmp_path / "fresh.db")
    migrate(fresh)
    with pytest.raises(backup.BackupError, match="different ENCRYPTION_KEY"):
        backup.restore(fresh, path, other)
    backup.restore(fresh, path, other, drop_secrets=True)
    assert fresh.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3
    assert fresh.execute("SELECT COUNT(*) FROM settings WHERE is_secret = 1").fetchone()[0] == 0


def test_restore_rejects_junk(env, tmp_path):
    conn, store = env
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all")
    with pytest.raises(backup.BackupError, match="isn't a PodBridge backup"):
        backup.restore(conn, junk, store._box)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (y)").connection.commit()
    with pytest.raises(backup.BackupError, match="tables are missing"):
        backup.inspect(other, store._box)


def test_backup_web_round_trip(app, authed):
    setup_library(app, authed)
    response = authed.get("/settings/backup")
    assert response.status_code == 200 and response.data[:16] == backup.SQLITE_HEADER
    assert "attachment" in response.headers["Content-Disposition"]
    saved = response.data
    # Change something, then restore the backup over it.
    page = authed.get("/library/1/pc-podcast-bb")
    authed.post("/episodes/3/hide", data={"csrf_token": csrf_from(page), "hidden": "1"})
    settings = authed.get("/settings")
    html = authed.post("/settings/restore", data={"csrf_token": csrf_from(settings),
                                                  "backup": (io.BytesIO(saved), "podbridge.db")},
                       content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Restored: 1 sources and 3 episodes." in html
    assert "Audio Only" in authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)  # unhidden again
