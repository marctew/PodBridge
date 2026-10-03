"""One source feeding several Pocket Casts podcasts (The News Agents + The News Agents USA)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from test_youtube import CHANNEL, FakeYouTube

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_youtube_all
from podbridge.linking import link_source, refresh_all, set_manual_match
from podbridge.matching import PatreonSide, PocketSide, match_episodes_loose, route_by_suffix
from podbridge.pocketcasts import CatalogueEpisode, EpisodeState
from podbridge.settings_store import SettingsStore
from podbridge.sync import run_sync
from podbridge.youtube import HistoryItem, VideoDetails

UK, USA = "pc-tna", "pc-tna-usa"
TITLES = {UK: "The News Agents", USA: "The News Agents USA"}


def test_route_by_suffix():
    assert route_by_suffix("Andy Burnham's dirty political secret | The News Agents", TITLES) == UK
    assert route_by_suffix("Were the Republicans humiliated? | The News Agents USA", TITLES) == USA
    assert route_by_suffix("No suffix", TITLES) is None
    assert route_by_suffix("Something | Unknown Show", TITLES) is None


def test_suffix_routes_same_day_episodes_to_the_right_podcast():
    # Same day, one UK and one USA episode: without routing, dates alone can't separate them.
    videos = [PatreonSide(1, "Andy Burnham's dirty political secret | The News Agents", "2026-10-01T12:00:00Z", 2521),
              PatreonSide(2, "Were the Republicans just humiliated by a 'dirtbag'? | The News Agents USA",
                          "2026-10-01T13:00:00Z", 2420)]
    pocket = [PocketSide("uk-1", "Man City's millions: Burnham's dirty secret", "2026-10-01T05:00:00Z", 2900, UK),
              PocketSide("us-1", "Schmitt for brains", "2026-10-01T06:00:00Z", 2800, USA)]
    assert match_episodes_loose(videos, pocket, podcast_titles=TITLES) == {
        1: ("uk-1", "auto_date"), 2: ("us-1", "auto_date"),
    }


DETAILS = {
    "UKvideo0001": VideoDetails("UKvideo0001", "Andy Burnham's dirty political secret | The News Agents", CHANNEL,
                                "2026-10-01T12:00:00Z", 2521.0),
    "USvideo0002": VideoDetails("USvideo0002", "Were the Republicans just humiliated? | The News Agents USA",
                                CHANNEL, "2026-10-01T13:00:00Z", 2420.0),
}
HISTORY = [HistoryItem("UKvideo0001", DETAILS["UKvideo0001"].title, 50.0),
           HistoryItem("USvideo0002", DETAILS["USvideo0002"].title, 25.0)]


class TwoPodcastPC:
    """Fake Pocket Casts with a separate catalogue per podcast."""

    def __init__(self):
        self.catalogues = {
            UK: [CatalogueEpisode("uk-1", "Man City's millions: Burnham's dirty secret", "2026-10-01T05:00:00Z", 2900.0)],
            USA: [CatalogueEpisode("us-1", "Schmitt for brains", "2026-10-01T06:00:00Z", 2800.0)],
        }
        self.states = {UK: {}, USA: {"us-1": EpisodeState("us-1", 2, 100.0, 2800.0)}}
        self.updates: list[tuple] = []

    def check_login(self):
        return True

    def list_episodes(self, podcast_uuid):
        return list(self.catalogues[podcast_uuid]), False

    def episode_states(self, podcast_uuid):
        return dict(self.states[podcast_uuid])

    def update_episode(self, *args, **kwargs):
        self.updates.append((args, kwargs))


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "mp.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    with conn:
        conn.execute("UPDATE sources SET enabled = 0")
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id, kind) "
                     "VALUES ('YouTube: The News Agents', ?, '', 'youtube')", (CHANNEL,))
    link_source(conn, 2, [(UK, TITLES[UK]), (USA, TITLES[USA])])
    return conn, store


def test_one_channel_two_podcasts_end_to_end(env):
    conn, store = env
    store.set("dry_run", "0")
    pc = TwoPodcastPC()
    summary = run_sync(conn, store, None, pc, "manual", youtube=FakeYouTube(HISTORY, DETAILS))
    assert summary.status == "ok", summary.error
    writes = sorted((args[0], args[1], kwargs["position"], kwargs["status"]) for args, kwargs in pc.updates)
    assert writes == [("uk-1", UK, 1260, 2), ("us-1", USA, 605, 2)]  # each written to its own podcast


def test_unticking_a_podcast_keeps_the_other_matches(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube(HISTORY, DETAILS))
    refresh_all(conn, store, TwoPodcastPC())
    link_source(conn, 2, [(UK, TITLES[UK])])
    rows = dict(conn.execute("SELECT patreon_post_id, pocketcasts_episode_uuid FROM episodes WHERE source_id = 2"))
    assert rows == {"UKvideo0001": "uk-1", "USvideo0002": None}
    link_source(conn, 2, [])
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE pocketcasts_episode_uuid IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT pocketcasts_podcast_uuid FROM sources WHERE id = 2").fetchone()[0] is None


def test_manual_match_accepts_any_linked_podcast(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube(HISTORY, DETAILS))
    refresh_all(conn, store, TwoPodcastPC())
    uk_ep = conn.execute("SELECT id FROM episodes WHERE patreon_post_id = 'UKvideo0001'").fetchone()[0]
    with conn:
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = NULL, match_method = 'none' "
                     "WHERE patreon_post_id = 'USvideo0002'")
    set_manual_match(conn, uk_ep, "us-1")  # deliberately cross-podcast: allowed by hand
    assert conn.execute("SELECT pocketcasts_episode_uuid FROM episodes WHERE id = ?", (uk_ep,)).fetchone()[0] == "us-1"


def test_migration_5_copies_existing_links(tmp_path):
    from podbridge.db import migrations
    conn = connect(tmp_path / "m5.db")
    for version, path in migrations():
        if version > 4:
            break
        conn.executescript(f"BEGIN;{path.read_text(encoding='utf-8')}PRAGMA user_version = {version};COMMIT;")
    with conn:
        conn.execute("UPDATE sources SET pocketcasts_podcast_uuid = 'pc-podcast-bb'")
    migrate(conn)
    assert [tuple(r) for r in conn.execute("SELECT source_id, podcast_uuid FROM source_podcasts")] == [
        (1, "pc-podcast-bb")]
