"""YouTube date backfill (public, cookieless), Fuzzy-match revalidation and Move here."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fakes import FakeResponse
from test_youtube import CHANNEL, CookieSession, FakeYouTube, page

from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import backfill_youtube_dates, discover_youtube_all, missing_youtube_dates
from podbridge.linking import (
    link_source, rematch_youtube_offline, store_catalogue, take_over_match,
)
from podbridge.pocketcasts import CatalogueEpisode
from podbridge.settings_store import SettingsStore
from podbridge.youtube import HttpYouTubeClient, PlaylistItem, PublicYouTubeClient, VideoDetails

GANGS_VIDEO = "GangsVideo1"
RADICAL_VIDEO = "RadicalVid1"
UPLOADS = {CHANNEL: [
    PlaylistItem(GANGS_VIDEO, "The Rise of Youth Gang Violence | The Crime Agents", 3352.0),
    PlaylistItem(RADICAL_VIDEO, "Why are CHILDREN being RADICALISED? | The Crime Agents", 3030.0),
]}
DETAILS = {
    GANGS_VIDEO: VideoDetails(GANGS_VIDEO, "The Rise of Youth Gang Violence | The Crime Agents", CHANNEL,
                              "2026-09-29T17:00:00Z", 3352.0),
    RADICAL_VIDEO: VideoDetails(RADICAL_VIDEO, "Why are CHILDREN being RADICALISED? | The Crime Agents", CHANNEL,
                                "2026-09-20T17:00:00Z", 3030.0),
}
CATALOGUE = [
    # Over a year older than the video it was fuzzily matched to.
    CatalogueEpisode("pc-inside", "Inside gangs: Why are children killing each other?", "2025-07-02T05:00:00Z", 3032.0),
    CatalogueEpisode("pc-radical", "Why are children being radicalised online?", "2026-09-20T05:00:00Z", 3100.0),
]


class FakePublic:
    def __init__(self, details):
        self.details, self.calls = details, []

    def video_details(self, video_id):
        self.calls.append(video_id)
        return self.details.get(video_id)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "d.db")
    migrate(conn)
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    with conn:
        conn.execute("UPDATE sources SET enabled = 0")
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id, kind) "
                     "VALUES ('YouTube: The Crime Agents', ?, '', 'youtube')", (CHANNEL,))
    link_source(conn, 2, [("pc-crime", "The Crime Agents")])
    with conn:
        store_catalogue(conn, "pc-crime", CATALOGUE, {})
    return conn, store


def matches(conn):
    return dict(conn.execute("SELECT patreon_post_id, pocketcasts_episode_uuid FROM episodes WHERE source_id = 2"))


def test_fuzzy_match_is_dropped_once_the_date_disagrees(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube([], {}, uploads=UPLOADS), detail_budget=0)
    with conn:  # simulate the bad no-date Fuzzy match from before
        conn.execute("UPDATE episodes SET pocketcasts_episode_uuid = 'pc-inside', match_method = 'auto_date' "
                     "WHERE patreon_post_id = ?", (RADICAL_VIDEO,))
    assert missing_youtube_dates(conn) == 2

    public = FakePublic(DETAILS)
    assert backfill_youtube_dates(conn, public) == 2
    assert missing_youtube_dates(conn) == 0
    rematch_youtube_offline(conn)
    assert matches(conn)[RADICAL_VIDEO] == "pc-radical"  # re-matched by date + title
    assert matches(conn)[GANGS_VIDEO] is None            # 'Inside gangs' is from 2025: no match


def test_backfill_respects_budget_and_cache(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube([], {}, uploads=UPLOADS), detail_budget=0)
    public = FakePublic(DETAILS)
    assert backfill_youtube_dates(conn, public, budget=1) == 1
    assert backfill_youtube_dates(conn, public, budget=5) == 1
    assert len(public.calls) == 2  # nothing refetched


def test_manual_match_survives_revalidation(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube([], {}, uploads=UPLOADS), detail_budget=0)
    episode_id = conn.execute("SELECT id FROM episodes WHERE patreon_post_id = ?", (GANGS_VIDEO,)).fetchone()[0]
    take_over_match(conn, episode_id, "pc-inside")
    backfill_youtube_dates(conn, FakePublic(DETAILS))
    rematch_youtube_offline(conn)
    assert matches(conn)[GANGS_VIDEO] == "pc-inside"  # manual: kept despite the date gap


def test_take_over_moves_the_match(env):
    conn, store = env
    discover_youtube_all(conn, store, FakeYouTube([], {}, uploads=UPLOADS), detail_budget=0)
    ids = dict(conn.execute("SELECT patreon_post_id, id FROM episodes"))
    take_over_match(conn, ids[RADICAL_VIDEO], "pc-inside")
    previous = take_over_match(conn, ids[GANGS_VIDEO], "pc-inside")
    assert previous == "Why are CHILDREN being RADICALISED? | The Crime Agents"
    assert matches(conn) == {GANGS_VIDEO: "pc-inside", RADICAL_VIDEO: None}
    locked = conn.execute("SELECT match_locked FROM episodes WHERE id = ?", (ids[RADICAL_VIDEO],)).fetchone()[0]
    assert locked == 0  # free to auto-match something else


def test_public_client_sends_only_the_consent_cookie():
    session = CookieSession(page('{"x":1}'))
    client = PublicYouTubeClient(session=session, gap_secs=0, sleep=lambda _s: None)
    assert {c.name: c.value for c in session.cookies} == {"SOCS": "CAI"}
    assert client.video_details("GangsVideo1") is None  # page had no player data
    assert session.calls[0]["url"] == "https://www.youtube.com/watch?v=GangsVideo1"


def test_signed_in_client_delegates_public_pages():
    public = FakePublic(DETAILS)
    client = HttpYouTubeClient("SID=x", lambda _t: None, session=CookieSession(), public=public)
    assert client.video_details(GANGS_VIDEO).channel_id == CHANNEL
    assert public.calls == [GANGS_VIDEO]


def test_public_consent_redirect_is_an_error_not_an_expired_login():
    from podbridge.youtube import YouTubeError, YouTubeSessionExpired
    session = CookieSession(page("", url="https://consent.youtube.com/m"))
    client = PublicYouTubeClient(session=session, gap_secs=0, sleep=lambda _s: None)
    with pytest.raises(YouTubeError) as excinfo:
        client.video_details("GangsVideo1")
    assert not isinstance(excinfo.value, YouTubeSessionExpired)


def test_web_move_here_and_fetch_dates(app, authed):
    from conftest import csrf_from
    from fakes import FakePocketCastsClient
    from test_web_patreon import post
    from test_web_pocketcasts import configure_pc, use_pc
    app.extensions["youtube_client_factory"] = lambda _s: FakeYouTube([], {}, uploads=UPLOADS)
    app.extensions["youtube_public_factory"] = lambda: FakePublic(DETAILS)
    use_pc(app, FakePocketCastsClient(catalogue=CATALOGUE))
    configure_pc(authed)
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1", youtube_cookies="SID=x")
    post(authed, "/sources/1/toggle", page="/sources")
    post(authed, "/sources/youtube", page="/sources", channel="@TheCrimeAgents")
    post(authed, "/sources/2/link", page="/sources", podcast_uuid="pc-crime")
    post(authed, "/episodes/refresh")

    sources = authed.get("/sources").get_data(as_text=True)
    assert "without a publish date yet" in sources
    html = post(authed, "/sources/youtube/dates", page="/sources").get_data(as_text=True)
    import re
    flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</', html, re.S)
    assert any("Filled in 2 YouTube dates" in f for f in flashes), flashes
    assert "without a publish date yet" not in html

    ids = {}
    with app.app_context():
        from podbridge.db import get_db
        ids = dict(get_db().execute("SELECT patreon_post_id, id FROM episodes"))
    match_page = authed.get(f"/episodes/{ids[GANGS_VIDEO]}/match")
    assert "Move here" in match_page.get_data(as_text=True)
    response = authed.post(f"/episodes/{ids[GANGS_VIDEO]}/match", data={
        "csrf_token": csrf_from(match_page), "action": "take_over", "uuid": "pc-radical", "next": "/episodes"},
        follow_redirects=True)
    assert "Matched here, and unmatched" in response.get_data(as_text=True)
