"""Episode discovery into the database, using a fake Patreon client."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from cryptography.fernet import Fernet
from fakes import FakePatreonClient

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.patreon import PatreonSessionExpired, parse_posts_page
from podbridge.settings_store import SettingsStore


def fixture_posts():
    page1, _ = parse_posts_page(fixture_json("patreon_posts_page1.json"))
    page2, _ = parse_posts_page(fixture_json("patreon_posts_page2.json"))
    return page1 + page2


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "d.db")
    migrate(conn)
    return conn, SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))


def test_discovery_inserts_episodes_and_progress(env):
    conn, store = env
    client = FakePatreonClient(fixture_posts())
    [result] = discover_all(conn, store, client)
    assert (result.posts_seen, result.added, result.refreshed, result.skipped_no_media) == (4, 3, 0, 1)
    assert client.list_calls == [("14434926", "1909234")]
    rows = conn.execute(
        "SELECT e.patreon_post_id, e.patreon_media_id, e.match_method, p.patreon_position_secs, "
        "p.patreon_is_watched, p.patreon_watch_state FROM episodes e JOIN progress p ON p.episode_id = e.id "
        "ORDER BY e.published_at DESC"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("171048709", "755348961", "none", 381.98, 0, "is_watching"),
        ("170000002", "755000002", "none", 2102.0, 1, "is_watched"),
        ("170000003", "755000003", "none", None, 0, "is_not_watched"),
    ]
    assert store.get("patreon_status") == "ok"
    assert store.get("patreon_verified_at")


def test_rediscovery_refreshes_without_duplicates_or_touching_matches(env):
    conn, store = env
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    with conn:
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = 'pc-1', match_method = 'manual' "
                     "WHERE patreon_post_id = '171048709'")
    [result] = discover_all(conn, store, FakePatreonClient(fixture_posts()))
    assert (result.added, result.refreshed) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3
    match = conn.execute("SELECT pocketcasts_episode_uuid, match_method FROM episodes "
                         "WHERE patreon_post_id = '171048709'").fetchone()
    assert tuple(match) == ("pc-1", "manual")


def test_dead_session_aborts_before_reading_posts(env):
    conn, store = env
    client = FakePatreonClient(fixture_posts(), logged_in=False)
    with pytest.raises(PatreonSessionExpired):
        discover_all(conn, store, client)
    assert client.list_calls == []
    assert store.get("patreon_status") == "expired"
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_disabled_sources_are_skipped(env):
    conn, store = env
    with conn:
        conn.execute("UPDATE sources SET enabled = 0")
    client = FakePatreonClient(fixture_posts())
    assert discover_all(conn, store, client) == []
    assert client.list_calls == []
