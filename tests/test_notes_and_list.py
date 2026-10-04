"""Episode pages (show notes with clickable timestamps) and My List (with Pocket Casts stars)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import csrf_from
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_library import setup_library
from test_linking import fake_pocketcasts
from test_web_pocketcasts import use_pc

from podbridge import my_list, notes
from podbridge.crypto import SecretBox
from podbridge.db import connect, get_db, migrate
from podbridge.discovery import discover_all
from podbridge.linking import link_source, refresh_all
from podbridge.pocketcasts import PocketCastsError, find_show_notes
from podbridge.settings_store import SettingsStore

PEEP = "171048709"


# --- notes: HTML to text, and rendering ---

def test_html_to_text_keeps_structure_and_drops_markup():
    html = ("<p>Hello&nbsp;<b>there</b></p><script>alert(1)</script><ul><li>One</li><li>Two</li></ul>"
            "<p>Line<br>break</p>")
    assert notes.html_to_text(html) == "Hello there\n\n• One\n\n• Two\n\nLine\nbreak"
    assert notes.html_to_text(None) == ""


EPISODE = {"id": 7, "title": "Ep \"7\"", "duration_secs": 4000.0, "source_kind": "patreon", "source_name": "Patreon",
           "patreon_url": "https://www.patreon.com/posts/ep-7-123", "pc_web_url": "https://pocketcasts.com/podcasts/p/e",
           "pc_app_url": "https://pca.st/episode/e?t=99"}


def test_render_links_timestamps_within_the_episode_and_urls():
    html = str(notes.render("Intro 00:00\nChapter 12:30 and 1:02:45\nAt 2:15:00 <b>x</b>\nSee https://example.com/a?b=1.",
                            EPISODE, "t"))
    assert 'href="https://www.patreon.com/posts/ep-7-123?t=750"' in html            # 12:30
    assert 'data-pc-direct="https://pca.st/episode/e?t=3765"' in html                # 1:02:45
    assert 'data-position-label="Start at 12:30"' in html
    assert ">2:15:00<" not in html and "2:15:00" in html                              # past the end: plain text
    assert "&lt;b&gt;x&lt;/b&gt;" in html                                             # text is escaped, never HTML
    assert '<a href="https://example.com/a?b=1"' in html and "</a>." in html          # trailing dot not in link
    assert 'data-title="Ep &quot;7&quot;"' in html
    clock = str(notes.render("Recorded 10:30am, live 9:15 PM", EPISODE, "t"))
    assert "<a" not in clock


def test_render_youtube_uses_t_and_skips_sheet_without_a_match():
    ep = {**EPISODE, "source_kind": "youtube", "source_name": "YouTube",
          "patreon_url": "https://www.youtube.com/watch?v=abc", "pc_web_url": None}
    html = str(notes.render("Go to 5:00", ep, "start"))
    assert 'href="https://www.youtube.com/watch?v=abc&amp;t=300"' in html and "data-choose" not in html


def test_find_show_notes_walks_the_bundle():
    body = {"podcast": {"episodes": [{"uuid": "a", "show_notes": "A"}, {"uuid": "b", "show_notes": "<p>B</p>"}]}}
    assert find_show_notes(body, "b") == "<p>B</p>" and find_show_notes(body, "z") is None


# --- My List ordering ---

@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    link_source(conn, 1, [("pc-podcast-bb", "Button Boys")])
    return conn, store


def test_list_add_move_reorder_remove(env):
    conn, _ = env
    for i in (1, 2, 3):
        my_list.add(conn, i)
    my_list.add(conn, 1)  # already there: no change
    assert my_list.ids(conn) == [1, 2, 3]
    my_list.move(conn, 3, -1)
    assert my_list.ids(conn) == [1, 3, 2]
    my_list.reorder(conn, [2, 99, 1])  # unknown IDs ignored, missing ones kept after
    assert my_list.ids(conn) == [2, 1, 3]
    my_list.remove(conn, 1)
    assert my_list.ids(conn) == [2, 3]


# --- Pocket Casts stars mirror into My List ---

def peep_id(conn):
    return conn.execute("SELECT id FROM episodes WHERE patreon_post_id = ?", (PEEP,)).fetchone()[0]


def starred(pc, uuid, value):
    pc.states[uuid] = replace(pc.states[uuid], starred=value)
    return pc


def test_stars_added_and_removed_in_pocketcasts_follow_into_my_list(env):
    conn, store = env
    pc = fake_pocketcasts()
    refresh_all(conn, store, pc)
    assert my_list.ids(conn) == []
    refresh_all(conn, store, starred(pc, "pc-ep-peep", True))
    assert my_list.ids(conn) == [peep_id(conn)]
    refresh_all(conn, store, pc)  # unchanged star: still there
    assert my_list.ids(conn) == [peep_id(conn)]
    refresh_all(conn, store, starred(pc, "pc-ep-peep", False))
    assert my_list.ids(conn) == []


def test_removing_in_podbridge_is_not_undone_by_an_unchanged_star(env):
    conn, store = env
    pc = starred(fake_pocketcasts(), "pc-ep-peep", True)
    refresh_all(conn, store, pc)
    my_list.remove(conn, peep_id(conn))  # e.g. the unstar write failed
    refresh_all(conn, store, pc)
    assert my_list.ids(conn) == []


def test_first_sighting_of_a_star_adds_it(env):
    conn, store = env
    refresh_all(conn, store, starred(fake_pocketcasts(), "pc-ep-peep", True))
    assert my_list.ids(conn) == [peep_id(conn)]


# --- pages ---

def library_with_notes(app, authed, pc=None):
    setup_library(app, authed)
    pc = pc or fake_pocketcasts()
    use_pc(app, pc)
    patreon = FakePatreonClient(fixture_posts())
    patreon.post_texts[PEEP] = "<p>Chapters</p><p>00:00 Intro<br>10:05 The peep</p>"
    app.extensions["patreon_client_factory"] = lambda _s: patreon
    with app.app_context():
        ep_id = peep_id(get_db())
    return ep_id, patreon, pc


def test_episode_page_fetches_notes_once_and_links_timestamps(app, authed):
    ep_id, patreon, pc = library_with_notes(app, authed)
    pc.notes["pc-ep-peep"] = "<p>Listen at 10:05.</p>"
    html = authed.get(f"/episodes/{ep_id}").get_data(as_text=True)
    assert "Try Not to Peep" in html and "The peep" in html
    assert "t=605" in html and "data-choose" in html
    assert "Pocket Casts show notes" in html and "Listen at" in html
    authed.get(f"/episodes/{ep_id}")
    assert patreon.list_calls.count(("text", PEEP)) == 1  # cached after the first view
    show = authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)
    assert f'href="/episodes/{ep_id}"' in show  # titles link to the page
    assert authed.get("/episodes/99999").status_code == 404


def test_episode_page_survives_note_failures(app, authed):
    ep_id, patreon, _ = library_with_notes(app, authed, pc=fake_pocketcasts(error=PocketCastsError("down")))
    patreon.error = RuntimeError("boom")
    html = authed.get(f"/episodes/{ep_id}").get_data(as_text=True)
    assert "No post text available" in html


def test_star_toggle_stars_in_pocketcasts_and_shows_on_home(app, authed):
    ep_id, _, pc = library_with_notes(app, authed)
    page = authed.get(f"/episodes/{ep_id}")
    html = authed.post(f"/episodes/{ep_id}/list", data={"csrf_token": csrf_from(page), "in_list": "1",
                                                         "next": f"/episodes/{ep_id}"},
                       follow_redirects=True).get_data(as_text=True)
    assert "starred in Pocket Casts" in html and "★ In My List" in html
    assert pc.stars == [("pc-ep-peep", "pc-podcast-bb", True)]
    home = authed.get("/").get_data(as_text=True)
    assert "My List" in home and "Edit list" in home
    listing = authed.get("/list").get_data(as_text=True)
    assert "Try Not to Peep" in listing and "Remove" in listing
    # The next refresh doesn't undo or duplicate it (the star now matches).
    authed.post("/episodes/refresh", data={"csrf_token": csrf_from(page)})
    with app.app_context():
        assert my_list.ids(get_db()) == [ep_id]


def test_a_star_pocketcasts_ignores_does_not_empty_my_list(app, authed):
    ep_id, _, pc = library_with_notes(app, authed)
    pc.set_starred = lambda *_args: None  # accepted, but nothing changes in Pocket Casts
    token = csrf_from(authed.get("/list"))
    authed.post(f"/episodes/{ep_id}/list", data={"csrf_token": token, "in_list": "1"})
    authed.post("/episodes/refresh", data={"csrf_token": token})
    with app.app_context():
        assert my_list.ids(get_db()) == [ep_id]


def test_star_failure_still_saves_to_my_list(app, authed):
    ep_id, _, pc = library_with_notes(app, authed)
    pc.star_error = PocketCastsError("nope")
    page = authed.get("/list")
    html = authed.post(f"/episodes/{ep_id}/list", data={"csrf_token": csrf_from(page), "in_list": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert "couldn&#39;t star it in Pocket Casts" in html and "Try Not to Peep" in html


def test_list_reorder_and_move_routes(app, authed):
    setup_library(app, authed)
    with app.app_context():
        db = get_db()
        with db:
            for i in (1, 2, 3):
                my_list.add(db, i)
    token = csrf_from(authed.get("/list"))
    response = authed.post("/list/order", data={"csrf_token": token, "order": "3,1,2"},
                           headers={"X-Requested-With": "fetch"})
    assert response.get_json() == {"ok": True}
    authed.post("/list/2/move", data={"csrf_token": token, "direction": "up"})
    with app.app_context():
        assert my_list.ids(get_db()) == [3, 2, 1]
    assert authed.post("/list/order", data={"csrf_token": token, "order": "x"}).status_code == 400


def test_open_with_sheet_links_to_episode_info(app, authed):
    ep_id, _, _ = library_with_notes(app, authed)
    home = authed.get("/").get_data(as_text=True)
    assert f'data-info="/episodes/{ep_id}"' in home and 'id="open-with-info"' in home
    page = authed.get(f"/episodes/{ep_id}").get_data(as_text=True)
    assert "data-info=" not in page  # already on it
