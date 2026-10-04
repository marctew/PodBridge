"""Auto-hide rules and "New" markers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import csrf_from
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_library import setup_library
from test_linking import fake_pocketcasts

from podbridge import hide_rules
from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.library import build_shows, new_this_week
from podbridge.linking import link_source, refresh_all
from podbridge.settings_store import SettingsStore
from podbridge.sync import run_sync

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "r.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    link_source(conn, 1, [("pc-podcast-bb", "Button Boys")])
    refresh_all(conn, store, fake_pocketcasts())
    return conn, store


def visible(conn):
    return {r[0] for r in conn.execute("SELECT patreon_post_id FROM episodes WHERE hidden = 0")}


# --- rules ---

def test_validate():
    assert hide_rules.validate("shorter_than", " 3 ") == "3"
    assert hide_rules.validate("title_contains", " Trailer ") == "Trailer"
    for kind, value in (("shorter_than", "abc"), ("shorter_than", "0"), ("title_contains", ""), ("nope", "x")):
        with pytest.raises(hide_rules.RuleError):
            hide_rules.validate(kind, value)


def test_title_rule_hides_and_delete_unhides(env):
    conn, _ = env
    rule, caught = hide_rules.add_rule(conn, 1, "title_contains", "audio ONLY")
    assert caught == 1 and "170000003" not in visible(conn)
    assert hide_rules.caught_counts(conn) == {rule.id: 1}
    assert hide_rules.delete_rule(conn, rule.id) == 1
    assert "170000003" in visible(conn)


def test_shorter_than_rule(env):
    conn, _ = env
    _, caught = hide_rules.add_rule(conn, 1, "shorter_than", "31")  # Audio Only is 30:00
    assert caught == 1 and "170000003" not in visible(conn)


def test_unmatched_after_days_rule(env):
    conn, _ = env
    with conn:
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none', "
                     "created_at = '2026-09-20T00:00:00Z' WHERE patreon_post_id = '170000003'")
    with conn:
        conn.execute("INSERT INTO hide_rules (id, source_id, kind, value) VALUES (99, 1, 'unmatched_after_days', '7')")
    rule = hide_rules.Rule(99, 1, "unmatched_after_days", "7")
    assert hide_rules.apply_rules(conn, [rule], now=NOW) == 1
    assert hide_rules.apply_rules(conn, [rule], now=NOW) == 0  # already hidden


def test_unhiding_by_hand_wins(app, authed):
    setup_library(app, authed)
    with app.app_context():
        from podbridge.db import get_db
        hide_rules.add_rule(get_db(), 1, "title_contains", "audio")
    page = authed.get("/library/1/pc-podcast-bb?filter=hidden")
    authed.post("/episodes/3/hide", data={"csrf_token": csrf_from(page), "hidden": "0"})
    with app.app_context():
        from podbridge.db import get_db
        assert hide_rules.apply_rules(get_db()) == 0  # rule leaves it alone now
    assert "Audio Only" in authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)


def test_sync_applies_rules_and_skips_hidden(env):
    conn, store = env
    with conn:
        conn.execute("INSERT INTO hide_rules (source_id, kind, value) VALUES (1, 'title_contains', 'peep')")
    store.set("dry_run", "0")
    from podbridge.patreon import Progress
    ahead = [replace(p, progress=Progress(2444.5, False, "is_watching", "2026-10-03T13:00:00+00:00"))
             if p.post_id == "171048709" else p for p in fixture_posts()]
    pc = fake_pocketcasts()
    run_sync(conn, store, FakePatreonClient(ahead), pc, "full")
    assert "171048709" not in visible(conn) and pc.updates == []


def test_rules_page(app, authed):
    setup_library(app, authed)
    page = authed.get("/sources/1/rules")
    html = authed.post("/sources/1/rules", data={"csrf_token": csrf_from(page), "kind": "title_contains",
                                                 "value": "audio"}, follow_redirects=True).get_data(as_text=True)
    assert "hid 1 episode." in html
    assert "hiding 1 episode" in html
    assert "Hide rules (1)" in authed.get("/sources").get_data(as_text=True)
    bad = authed.post("/sources/1/rules", data={"csrf_token": csrf_from(page), "kind": "shorter_than",
                                                "value": "lots"}, follow_redirects=True).get_data(as_text=True)
    assert "Enter a number." in bad


# --- New markers ---

def test_visits_track_previous_start(app, authed):
    with app.app_context():
        from podbridge.settings_store import get_store
        store = get_store()
        store.set("last_visit_at", "2026-01-01T00:00:00Z")  # long ago: this request starts a new visit
    authed.get("/")
    with app.app_context():
        from podbridge.settings_store import get_store
        store = get_store()
        assert store.get("previous_visit_at") == "2026-01-01T00:00:00Z"
        assert store.get("last_visit_at") > "2026-01-01T00:00:00Z"


def test_new_marks_recent_unwatched_episodes_first_seen_since_last_visit(env):
    conn, _ = env
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:  # Audio Only: unwatched, published 2 days ago, first seen just now
        conn.execute("UPDATE episodes SET published_at = ?, created_at = ? WHERE patreon_post_id = '170000003'",
                     (recent, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    [show] = build_shows(conn, "t", new_since=since)
    flags = {e["patreon_post_id"]: e["is_new"] for e in show.episodes}
    assert flags == {"171048709": False, "170000002": False, "170000003": True}
    assert show.new_count == 1
    assert [e["patreon_post_id"] for e in new_this_week([show])] == ["170000003"]
    # Seen before the last visit: no longer new.
    [show] = build_shows(conn, "t", new_since=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert show.new_count == 0


def test_old_back_catalogue_is_never_new(env):
    conn, _ = env
    just_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:  # first seen just now (a fresh import), but published a month ago
        conn.execute("UPDATE episodes SET created_at = ?, published_at = ? WHERE patreon_post_id = '170000003'",
                     (just_now, month_ago))
    [show] = build_shows(conn, "t", new_since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert show.new_count == 0
