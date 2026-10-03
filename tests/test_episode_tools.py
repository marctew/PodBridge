"""Up next, library search, Sync now for one episode, Mark played/unplayed, and the app icon."""

from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_library import setup_library
from test_linking import fake_pocketcasts
from test_web_patreon import post

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.library import build_shows, fold, search, up_next
from podbridge.linking import link_source, refresh_all
from podbridge.patreon import Progress
from podbridge.pocketcasts import EpisodeState
from podbridge.settings_store import SettingsStore
from podbridge.sync import SyncBusy, _lock, set_played, sync_one_episode

PEEP = "171048709"


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    link_source(conn, 1, [("pc-podcast-bb", "Button Boys")])
    refresh_all(conn, store, fake_pocketcasts())
    return conn, store


def episode_id(conn, post_id):
    return conn.execute("SELECT id FROM episodes WHERE patreon_post_id = ?", (post_id,)).fetchone()[0]


def pc_state(conn, post_id):
    return tuple(conn.execute("SELECT p.pocketcasts_status, p.pocketcasts_position_secs FROM progress p "
                              "JOIN episodes e ON e.id = p.episode_id WHERE e.patreon_post_id = ?", (post_id,)).fetchone())


# --- Up next and search ---

def test_up_next_is_the_episode_after_the_newest_touched(env):
    conn, _ = env
    # Fixture: Pierre's Dad (24 Sep) played, Try Not to Peep (1 Oct) in progress, audio (17 Sep) unwatched.
    assert up_next(build_shows(conn, "t")) == []  # nothing unwatched after the newest touched episode
    with conn:  # rewind history: only the oldest is played
        conn.execute("UPDATE progress SET patreon_position_secs = NULL, patreon_is_watched = 0, "
                     "pocketcasts_status = 1, pocketcasts_position_secs = 0 WHERE episode_id != ?",
                     (episode_id(conn, "170000003"),))
        conn.execute("UPDATE progress SET pocketcasts_status = 3 WHERE episode_id = ?", (episode_id(conn, "170000003"),))
    [nxt] = up_next(build_shows(conn, "t"))
    assert nxt["patreon_post_id"] == "170000002"  # the next one published after it


def test_fold_and_search(env):
    conn, _ = env
    assert fold("Where’s Pierre’s Dad?") == "wheres pierres dad?"
    shows, episodes = search(build_shows(conn, "t"), "pierres DAD")
    assert [e["patreon_post_id"] for e in episodes] == ["170000002"]
    shows, episodes = search(build_shows(conn, "t"), "button boys")
    assert [s.title for s in shows] == ["Button Boys"] and len(episodes) == 3  # show title matches too
    assert search(build_shows(conn, "t"), "   ") == ([], [])


# --- Sync now for one episode ---

def test_sync_one_episode_uses_fresh_state_and_ignores_unchanged(env):
    conn, store = env
    store.set("dry_run", "0")
    ahead = [replace(p, progress=Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00"))
             if p.post_id == PEEP else p for p in fixture_posts()]
    pc = fake_pocketcasts()
    pc.states["pc-ep-peep"] = EpisodeState("pc-ep-peep", 2, 1300.0, 2838.0)  # listened since the last sync
    summary = sync_one_episode(conn, store, episode_id(conn, PEEP), pc, patreon=FakePatreonClient(ahead))
    assert summary.status == "ok" and pc.updates == [("pc-ep-peep", "pc-podcast-bb", 2444, 2838, 2)]

    pc2 = fake_pocketcasts()
    pc2.states["pc-ep-peep"] = EpisodeState("pc-ep-peep", 2, 100.0, 2838.0)  # rewound in Pocket Casts
    sync_one_episode(conn, store, episode_id(conn, PEEP), pc2, patreon=FakePatreonClient(ahead))
    assert pc2.updates == [("pc-ep-peep", "pc-podcast-bb", 2444, 2838, 2)]  # forced despite "unchanged"


def test_sync_one_episode_checks_the_session_first(env):
    conn, store = env
    summary = sync_one_episode(conn, store, episode_id(conn, PEEP), fake_pocketcasts(),
                               patreon=FakePatreonClient(fixture_posts(), logged_in=False))
    assert summary.status == "aborted"


def test_sync_one_episode_respects_dry_run(env):
    conn, store = env  # dry run on by default
    ahead = [replace(p, progress=Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00"))
             if p.post_id == PEEP else p for p in fixture_posts()]
    pc = fake_pocketcasts()
    summary = sync_one_episode(conn, store, episode_id(conn, PEEP), pc, patreon=FakePatreonClient(ahead))
    assert summary.dry_run and pc.updates == []


# --- Mark played / unplayed ---

def test_mark_played_and_unplayed(env):
    conn, _ = env
    pc = fake_pocketcasts()
    assert "played" in set_played(conn, episode_id(conn, PEEP), True, pc)
    assert pc.updates[-1] == ("pc-ep-peep", "pc-podcast-bb", 2838, 2838, 3)
    assert pc_state(conn, PEEP) == (3, 2838.0)
    set_played(conn, episode_id(conn, PEEP), False, pc)
    assert pc.updates[-1] == ("pc-ep-peep", "pc-podcast-bb", 0, 2838, 1)
    assert pc_state(conn, PEEP) == (1, 0.0)


def test_mark_unplayed_is_not_undone_by_the_next_sync(env):
    from podbridge.sync import run_sync
    conn, store = env
    store.set("dry_run", "0")
    set_played(conn, episode_id(conn, "170000002"), False, fake_pocketcasts())  # Pierre's Dad: watched on Patreon
    pc = fake_pocketcasts()
    pc.states.pop("pc-ep-dad")  # Pocket Casts now reports it unplayed
    run_sync(conn, store, FakePatreonClient(fixture_posts()), pc, "full")
    assert not [u for u in pc.updates if u[0] == "pc-ep-dad"]  # Patreon unchanged: your choice stands


def test_tools_refuse_while_a_sync_runs(env):
    conn, _ = env
    _lock.acquire()
    try:
        with pytest.raises(SyncBusy):
            set_played(conn, episode_id(conn, PEEP), True, fake_pocketcasts())
    finally:
        _lock.release()


def test_unmatched_episode_cannot_be_marked(env):
    conn, _ = env
    with conn:
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none'")
    with pytest.raises(ValueError):
        set_played(conn, episode_id(conn, PEEP), True, fake_pocketcasts())


# --- pages ---

def test_pages_show_tools_search_and_up_next(app, authed):
    setup_library(app, authed)
    show = authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)
    assert "↻ Sync now" in show and "✓ Mark played" in show and "Mark unplayed" in show
    results = authed.get("/library?q=pierre").get_data(as_text=True)
    assert "Hidden Cache - Where" in results and "Try Not to Peep" not in results


def test_mark_played_route_returns_to_the_episode(app, authed):
    from conftest import csrf_from
    setup_library(app, authed)
    page = authed.get("/library/1/pc-podcast-bb")
    response = authed.post("/episodes/1/played", data={"csrf_token": csrf_from(page), "played": "1",
                                                       "next": "/library/1/pc-podcast-bb#episode-1"})
    assert response.headers["Location"] == "/library/1/pc-podcast-bb#episode-1"
    assert "Marked played in Pocket Casts" in authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)


def test_sync_now_route(app, authed):
    from conftest import csrf_from
    setup_library(app, authed)
    page = authed.get("/library/1/pc-podcast-bb")
    html = authed.post("/episodes/1/sync", data={"csrf_token": csrf_from(page), "next": "/library/1/pc-podcast-bb"},
                       follow_redirects=True).get_data(as_text=True)
    assert "isn&#39;t ahead of Pocket Casts" in html or "Dry run" in html


def test_icons_and_manifest_are_public(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'rel="apple-touch-icon"' in html and 'rel="manifest"' in html
    icon = client.get("/static/icons/apple-touch-icon.png")
    assert icon.status_code == 200 and icon.data[:8] == b"\x89PNG\r\n\x1a\n"
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.get_json()["short_name"] == "PodBridge"
