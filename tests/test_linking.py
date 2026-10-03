"""Catalogue import, auto-matching into the DB, manual match and unlink."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from cryptography.fernet import Fernet
from fakes import FakePatreonClient, FakePocketCastsClient
from test_discovery import fixture_posts

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.linking import MatchError, link_source, refresh_all, set_manual_match, unlink
from podbridge.pocketcasts import parse_catalogue, parse_states
from podbridge.settings_store import SettingsStore


def fake_pocketcasts(**kwargs):
    catalogue, _ = parse_catalogue(fixture_json("pocketcasts_catalogue.json"))
    states = parse_states(fixture_json("pocketcasts_states.json"))
    return FakePocketCastsClient(catalogue=catalogue, states=states, **kwargs)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "l.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    link_source(conn, 1, "pc-podcast-bb")
    return conn, store


def matches(conn):
    return {r["patreon_post_id"]: (r["pocketcasts_episode_uuid"], r["match_method"], r["pocketcasts_status"],
                                   r["pocketcasts_position_secs"])
            for r in conn.execute("SELECT e.*, p.pocketcasts_status, p.pocketcasts_position_secs FROM episodes e "
                                  "LEFT JOIN progress p ON p.episode_id = e.id")}


def test_refresh_matches_and_imports_state(env):
    conn, store = env
    [result] = refresh_all(conn, store, fake_pocketcasts())
    assert (result.catalogue_size, result.newly_matched, result.matched_total, result.unmatched) == (5, 3, 3, 0)
    assert matches(conn) == {
        "171048709": ("pc-ep-peep", "auto_title", 2, 1200.0),      # in progress in Pocket Casts
        "170000002": ("pc-ep-dad", "auto_title", 3, 2102.0),       # curly apostrophes normalised
        "170000003": ("pc-ep-audio", "auto_duration", 1, 0.0),     # renamed in feed; date + length
    }
    assert store.get("pocketcasts_status") == "ok"
    assert conn.execute("SELECT COUNT(*) FROM pocketcasts_episodes").fetchone()[0] == 5


def test_refresh_is_idempotent_and_keeps_manual_matches(env):
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    unlink(conn, 3)  # the audio post, by episode id
    set_manual_match(conn, 3, "pc-ep-old")
    [result] = refresh_all(conn, store, fake_pocketcasts())
    assert result.newly_matched == 0
    assert matches(conn)["170000003"][:2] == ("pc-ep-old", "manual")


def test_unlinked_episode_is_not_rematched(env):
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    unlink(conn, 1)
    refresh_all(conn, store, fake_pocketcasts())
    assert matches(conn)["171048709"] == (None, "none", None, None)


def test_manual_match_rejects_taken_and_foreign_episodes(env):
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    with pytest.raises(MatchError, match="Already matched"):
        set_manual_match(conn, 3, "pc-ep-peep")
    with pytest.raises(MatchError, match="isn't in this source"):
        set_manual_match(conn, 3, "some-other-uuid")


def test_relinking_source_to_another_podcast_clears_matches(env):
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    link_source(conn, 1, "pc-podcast-public")
    assert all(v == (None, "none", None, None) for v in matches(conn).values())


def test_unlinked_sources_are_skipped(env):
    conn, store = env
    conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = NULL")
    conn.commit()
    assert refresh_all(conn, store, fake_pocketcasts()) == []
