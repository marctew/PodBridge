"""YouTube support: page parsing, cookies, discovery, loose matching and sync."""

from __future__ import annotations

import json

import pytest
from conftest import fixture_json
from cryptography.fernet import Fernet
from fakes import FakePocketCastsClient, FakeResponse, FakeSession

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_youtube_all
from podbridge.linking import link_source
from podbridge.matching import PatreonSide, PocketSide, match_episodes_loose
from podbridge.pocketcasts import CatalogueEpisode, EpisodeState
from podbridge.settings_store import SettingsStore
from podbridge.sync import run_sync
from podbridge.youtube import (
    Channel, HistoryItem, HttpYouTubeClient, VideoDetails, YouTubeSessionExpired, extract_json, parse_cookies,
    parse_history, parse_watch_page,
)

CHANNEL = "UCnewsagents0000000000aa"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsapisid-secret-value\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-1PSID\tpsid-secret-value\n"
    ".example.com\tTRUE\t/\tFALSE\t0\ttracker\tnope\n"
)


# --- parsing ---

def test_parse_history_handles_both_layouts():
    items = parse_history(fixture_json("youtube_history.json"))
    assert items == [
        HistoryItem("NewsAgent01", "Is this the end for the PM? | The News Agents", 50.0),
        HistoryItem("NewsAgent02", "Trump's tariffs explained", 100.0),
        HistoryItem("OtherChan03", "Cat video", 30.0),
        HistoryItem("NoProgres04", "Never opened", None),
    ]


def test_extract_json_and_watch_page():
    player = {"videoDetails": {"videoId": "NewsAgent01", "title": "Is this the end?", "lengthSeconds": "2490",
                               "channelId": CHANNEL},
              "microformat": {"playerMicroformatRenderer": {"publishDate": "2026-10-02T06:00:00-07:00"}}}
    html = f"<script>var ytInitialPlayerResponse = {json.dumps(player)};var meta = 1;</script>"
    details = parse_watch_page(extract_json(html, "ytInitialPlayerResponse"), "NewsAgent01")
    assert details == VideoDetails("NewsAgent01", "Is this the end?", CHANNEL, "2026-10-02T13:00:00Z", 2490.0)
    assert extract_json("<html>nothing</html>", "ytInitialData") is None


def test_parse_cookies_formats():
    cookies = parse_cookies(COOKIES)
    assert [(c["name"], c["domain"]) for c in cookies] == [("SAPISID", ".youtube.com"),
                                                          ("__Secure-1PSID", ".youtube.com")]
    header = parse_cookies("Cookie: SID=abc; HSID=def")
    assert [(c["name"], c["value"]) for c in header] == [("SID", "abc"), ("HSID", "def")]


class CookieSession(FakeSession):
    """FakeSession with a real cookie jar, like requests.Session."""

    def __init__(self, *responses):
        from requests.cookies import RequestsCookieJar
        super().__init__(*responses)
        self.cookies = RequestsCookieJar()


def make_client(*responses):
    session = CookieSession(*responses)
    saved: list[str] = []
    client = HttpYouTubeClient(COOKIES, saved.append, session=session, gap_secs=0, sleep=lambda _s: None)
    return client, session, saved


def page(body: str, status: int = 200, url: str = "https://www.youtube.com/feed/history"):
    response = FakeResponse(status, None)
    response.text = body
    response.url = url
    return response


def test_cookies_are_loaded_and_sent_via_the_jar():
    client, session, _ = make_client(page('ytcfg.set({"LOGGED_IN":true})'))
    assert {c.name for c in session.cookies} == {"SAPISID", "__Secure-1PSID"}
    assert client.check_session() is True
    assert session.calls[0]["url"] == "https://www.youtube.com/feed/history"


def test_logged_out_and_consent_redirect():
    client, _, _ = make_client(page('ytcfg.set({"LOGGED_IN":false})'))
    assert client.check_session() is False
    client, _, _ = make_client(page("", url="https://consent.youtube.com/m?continue=x"))
    with pytest.raises(YouTubeSessionExpired, match="consent"):
        client.check_session()


def test_rotated_cookies_are_handed_back():
    from requests.cookies import create_cookie
    client, session, saved = make_client(page('"LOGGED_IN":true'), page('"LOGGED_IN":true'))
    session.cookies.set_cookie(create_cookie("__Secure-1PSIDTS", "rotated-value", domain=".youtube.com"))
    client.check_session()
    client.check_session()  # nothing new: no second save
    assert len(saved) == 1 and "rotated-value" in saved[0]


def test_history_page_end_to_end():
    data = fixture_json("youtube_history.json")
    html = f'<script>ytcfg.set({{"LOGGED_IN":true}});var ytInitialData = {json.dumps(data)};</script>'
    client, _, _ = make_client(page(html))
    assert [i.video_id for i in client.history()][:2] == ["NewsAgent01", "NewsAgent02"]


def test_cookies_without_youtube_entries_are_rejected():
    with pytest.raises(YouTubeSessionExpired):
        HttpYouTubeClient(".example.com\tTRUE\t/\tFALSE\t0\tx\ty\n", lambda _t: None)


# --- loose matching ---

def test_loose_matching_by_date_and_similarity():
    videos = [PatreonSide(1, "Is this the end for the PM? | The News Agents", "2026-10-02T13:00:00Z", 2490),
              PatreonSide(2, "Trump's tariffs explained", "2026-10-01T13:00:00Z", 2280)]
    pocket = [PocketSide("p-pm", "Is this the end for the Prime Minister?", "2026-10-02T05:00:00Z", 2800),
              PocketSide("p-tariff", "Tariffs: what Trump wants", "2026-10-01T05:00:00Z", 2600),
              PocketSide("p-bonus", "Bonus: listener questions", "2026-10-01T18:00:00Z", 1200)]
    found = match_episodes_loose(videos, pocket)
    assert found[1] == ("p-pm", "auto_date")       # two within 30 h (p-pm, p-bonus); similarity decides
    assert found[2] == ("p-tariff", "auto_date")   # three within 30 h; reordered words still score highest


def test_channel_suffix_is_ignored_for_title_matching():
    from podbridge.matching import strip_channel_suffix
    assert strip_channel_suffix("WTF went on at RAF Fairford? | The News Agents") == "WTF went on at RAF Fairford?"
    assert strip_channel_suffix("No suffix here") == "No suffix here"
    videos = [PatreonSide(1, "WTF went on at RAF Fairford? | The News Agents", "2026-10-01T15:54:35Z", 3189)]
    pocket = [PocketSide("p1", "WTF went on at RAF Fairford?", "2026-09-29T05:00:00Z", 3500),  # outside 30 h
              PocketSide("p2", "Something else", "2026-10-01T05:00:00Z", 3000)]
    assert match_episodes_loose(videos, pocket) == {1: ("p1", "auto_title")}


def test_loose_matching_refuses_close_calls():
    videos = [PatreonSide(1, "Weekly roundup", "2026-10-01T12:00:00Z", 2000)]
    pocket = [PocketSide("a", "Monday news", "2026-10-01T05:00:00Z", 2000),
              PocketSide("b", "Tuesday news", "2026-10-01T20:00:00Z", 2000)]
    assert match_episodes_loose(videos, pocket) == {}


# --- discovery and sync with fakes ---

class FakeYouTube:
    def __init__(self, history, details, logged_in=True, uploads=None):
        self._history, self._details, self.logged_in = history, details, logged_in
        self.uploads = uploads or {}
        self.detail_calls: list[str] = []

    def channel_videos(self, channel_id):
        return list(self.uploads.get(channel_id, []))

    def check_session(self):
        return self.logged_in

    def history(self):
        return list(self._history)

    def video_details(self, video_id):
        self.detail_calls.append(video_id)
        return self._details.get(video_id)

    def resolve_channel(self, handle):
        return Channel(CHANNEL, "The News Agents")


DETAILS = {
    "NewsAgent01": VideoDetails("NewsAgent01", "Is this the end for the PM? | The News Agents", CHANNEL,
                                "2026-10-02T13:00:00Z", 2490.0),
    "NewsAgent02": VideoDetails("NewsAgent02", "Trump's tariffs explained", CHANNEL, "2026-10-01T13:00:00Z", 2280.0),
    "OtherChan03": VideoDetails("OtherChan03", "Cat video", "UCsomeoneelse000000000aa", "2026-09-01T00:00:00Z", 60.0),
}
CATALOGUE = [CatalogueEpisode("p-pm", "Is this the end for the Prime Minister?", "2026-10-02T05:00:00Z", 2800.0),
             CatalogueEpisode("p-tariff", "Tariffs: what Trump wants", "2026-10-01T05:00:00Z", 2600.0)]


@pytest.fixture
def yt_env(tmp_path):
    conn = connect(tmp_path / "y.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    with conn:
        conn.execute("UPDATE sources SET enabled = 0")  # the seeded Patreon source
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id, kind) "
                     "VALUES ('YouTube: The News Agents', ?, '', 'youtube')", (CHANNEL,))
    source_id = conn.execute("SELECT id FROM sources WHERE kind = 'youtube'").fetchone()[0]
    link_source(conn, source_id, "pc-news")
    return conn, store


def test_youtube_discovery_filters_channel_and_caches_details(yt_env):
    conn, store = yt_env
    yt = FakeYouTube(parse_history(fixture_json("youtube_history.json")), DETAILS)  # no uploads page
    [result] = discover_youtube_all(conn, store, yt)
    assert (result.posts_seen, result.added) == (0, 2)  # found via history alone
    rows = {r["patreon_post_id"]: (r["patreon_position_secs"], r["patreon_url"]) for r in conn.execute(
        "SELECT e.patreon_post_id, e.patreon_url, p.patreon_position_secs FROM episodes e "
        "JOIN progress p ON p.episode_id = e.id")}
    assert rows == {"NewsAgent01": (1245.0, "https://www.youtube.com/watch?v=NewsAgent01"),
                    "NewsAgent02": (2280.0, "https://www.youtube.com/watch?v=NewsAgent02")}
    # The item without a progress bar is never looked up.
    assert sorted(yt.detail_calls) == ["NewsAgent01", "NewsAgent02", "OtherChan03"]
    discover_youtube_all(conn, store, yt)
    assert len(yt.detail_calls) == 3  # all cached, including the other channel's video
    assert store.get("youtube_status") == "ok"


def test_youtube_progress_timestamp_only_moves_when_percent_changes(yt_env):
    conn, store = yt_env
    history = parse_history(fixture_json("youtube_history.json"))
    discover_youtube_all(conn, store, FakeYouTube(history, DETAILS))
    first = conn.execute("SELECT patreon_updated_at FROM progress").fetchall()
    discover_youtube_all(conn, store, FakeYouTube(history, DETAILS))
    assert conn.execute("SELECT patreon_updated_at FROM progress").fetchall() == first


def test_youtube_sync_end_to_end(yt_env):
    conn, store = yt_env
    store.set("dry_run", "0")
    yt = FakeYouTube(parse_history(fixture_json("youtube_history.json")), DETAILS)
    pc = FakePocketCastsClient(catalogue=CATALOGUE,
                               states={"p-pm": EpisodeState("p-pm", 2, 300.0, 2800.0)})
    summary = run_sync(conn, store, None, pc, "manual", youtube=yt)
    assert summary.status == "ok", summary.error
    assert sorted(pc.updates) == [
        ("p-pm", "pc-news", 1245, 2800, 2),       # 50% of the 41:30 video
        ("p-tariff", "pc-news", 2600, 2600, 3),   # 100% watched: played, written at the podcast's length
    ]


def test_dead_youtube_login_does_not_block_patreon(yt_env):
    conn, store = yt_env
    yt = FakeYouTube([], DETAILS, logged_in=False)
    summary = run_sync(conn, store, None, FakePocketCastsClient(catalogue=CATALOGUE), "full", youtube=yt)
    assert summary.status == "aborted" and "YouTube session expired" in summary.error
    assert store.get("youtube_status") == "expired"


# --- migration safety ---

def test_migration_4_keeps_existing_episodes_and_progress(tmp_path):
    from podbridge.db import migrations
    conn = connect(tmp_path / "m.db")
    for version, path in migrations():
        if version > 3:
            break
        conn.executescript(f"BEGIN;{path.read_text(encoding='utf-8')}PRAGMA user_version = {version};COMMIT;")
    with conn:
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title, pocketcasts_episode_uuid, "
                     "match_method) VALUES (1, 'p1', 'Kept', 'uuid-1', 'manual')")
        conn.execute("INSERT INTO progress (episode_id, patreon_position_secs) VALUES (1, 123.0)")
    migrate(conn)
    row = conn.execute("SELECT e.title, e.match_method, e.match_locked, p.patreon_position_secs FROM episodes e "
                       "JOIN progress p ON p.episode_id = e.id").fetchone()
    assert tuple(row) == ("Kept", "manual", 0, 123.0)
    assert conn.execute("SELECT kind FROM sources").fetchone()[0] == "patreon"
    with conn:
        conn.execute("UPDATE episodes SET match_method = 'auto_date'")  # new value allowed
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
