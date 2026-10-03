"""YouTube pages: Save & test, adding a channel, refresh, and Continue-on-YouTube links."""

from __future__ import annotations

from conftest import fixture_json
from fakes import FakePocketCastsClient
from test_web_patreon import post
from test_web_pocketcasts import configure_pc, use_pc
from test_youtube import CATALOGUE, CHANNEL, DETAILS, FakeYouTube

from podbridge.pocketcasts import Podcast
from podbridge.youtube import parse_history


def use_yt(app, yt):
    app.extensions["youtube_client_factory"] = lambda _store: yt


def test_save_and_test_youtube(app, authed):
    use_yt(app, FakeYouTube([], DETAILS))
    html = post(authed, "/settings", page="/settings", action="test_youtube", sync_interval_minutes="15",
                dry_run="1", youtube_cookies=".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx").get_data(as_text=True)
    assert "YouTube cookies are signed in." in html
    assert ".youtube.com\tTRUE" not in html  # write-only
    assert authed.get("/healthz").get_json()["youtube_session_valid"] is True


def test_add_channel_link_refresh_and_resume(app, authed):
    yt = FakeYouTube(parse_history(fixture_json("youtube_history.json")), DETAILS)
    use_yt(app, yt)
    use_pc(app, FakePocketCastsClient(
        podcasts=[Podcast("pc-other", "Button Boys", None, "www.patreon.com"),
                  Podcast("pc-news", "The News Agents", "Global", "feeds.example.com")],
        catalogue=CATALOGUE))
    configure_pc(authed)
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1", youtube_cookies="SID=x")
    post(authed, "/sources/1/toggle", page="/sources")  # disable the seeded Patreon source

    html = post(authed, "/sources/youtube", page="/sources", channel="@TheNewsAgents").get_data(as_text=True)
    assert "Added YouTube: The News Agents." in html
    link_page = authed.get("/sources/2/link").get_data(as_text=True)
    assert link_page.index("The News Agents") < link_page.index("Button Boys")  # closest name first
    post(authed, "/sources/2/link", page="/sources", podcast_uuid="pc-news")

    html = post(authed, "/episodes/refresh").get_data(as_text=True)
    assert "YouTube · YouTube: The News Agents: 0 recent uploads, 2 new" in html
    assert "2 matched (2 new)" in html
    assert "Continue on YouTube at 20:45" in html  # 50% of 41:30
    assert authed.get("/go/latest").headers["Location"].startswith("https://www.youtube.com/watch?v=NewsAgent01&t=1245")

    again = post(authed, "/sources/youtube", page="/sources", channel=CHANNEL).get_data(as_text=True)
    assert "already a source" in again
