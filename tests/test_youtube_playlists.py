"""YouTube playlists as sources (a podcast that lives on someone else's channel)."""

from __future__ import annotations

import json

from fakes import FakePocketCastsClient, FakeResponse, FakeSession
from test_web_patreon import post
from test_web_pocketcasts import configure_pc, use_pc
from test_web_youtube import use_yt
from test_youtube import CHANNEL, DETAILS, FakeYouTube, yt_env  # noqa: F401  (yt_env is a fixture)

from podbridge.discovery import discover_youtube_all
from podbridge.pocketcasts import Podcast
from podbridge.youtube import (
    HistoryItem, Playlist, PlaylistItem, PublicYouTubeClient, client_version, continuation_token, is_channel_id,
    parse_playlist_page, playlist_id_from,
)

PLAYLIST = "PLL0giNASK99c8ySbRFim7b-jKcsrwEHlJ"


def test_playlist_id_from_urls_and_ids():
    assert playlist_id_from(f"https://www.youtube.com/playlist?list={PLAYLIST}") == PLAYLIST
    assert playlist_id_from(f"youtube.com/playlist?list={PLAYLIST}&si=abc") == PLAYLIST
    assert playlist_id_from(f"https://www.youtube.com/watch?v=NewsAgent01&list={PLAYLIST}&index=3") == PLAYLIST
    assert playlist_id_from(f"https://m.youtube.com/playlist?list={PLAYLIST}") == PLAYLIST
    assert playlist_id_from(PLAYLIST) == PLAYLIST
    assert playlist_id_from("@TheNewsAgents") is None
    assert playlist_id_from(f"https://www.youtube.com/channel/{CHANNEL}") is None
    assert playlist_id_from("https://www.youtube.com/watch?v=x&list=RDNewsAgent01") is None  # a mix
    assert playlist_id_from("https://www.youtube.com/playlist?list=WL") is None              # Watch later
    assert is_channel_id(CHANNEL) and not is_channel_id(PLAYLIST)


def item(video_id, title, length="2400"):
    return {"playlistVideoRenderer": {"videoId": video_id, "title": {"simpleText": title}, "lengthSeconds": length}}


def more(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}}


def page_html(data, version="2.20261020.01.00"):
    response = FakeResponse(200)
    response.text = (f'<script>ytcfg.set({{"INNERTUBE_CLIENT_VERSION":"{version}"}});'
                     f"var ytInitialData = {json.dumps(data)};</script>")
    response.url = "https://www.youtube.com/playlist"
    return response


def browse(data):
    response = FakeResponse(200)
    response.text = json.dumps(data)
    response.url = "https://www.youtube.com/youtubei/v1/browse"
    return response


FIRST_PAGE = {
    "metadata": {"playlistMetadataRenderer": {"title": "Storytime Podcast"}},
    "contents": [item("Storytime01", "Episode 1"), item("Storytime02", "Episode 2"), more("tok-1")],
}


def test_parse_playlist_page_title_and_continuation():
    assert parse_playlist_page(FIRST_PAGE, PLAYLIST) == Playlist(PLAYLIST, "Storytime Podcast")
    micro = {"microformat": {"microformatDataRenderer": {"title": "From microformat"}}}
    assert parse_playlist_page(micro, PLAYLIST).title == "From microformat"
    assert parse_playlist_page({"alerts": [{"text": "This playlist does not exist."}]}, PLAYLIST) is None
    assert continuation_token(FIRST_PAGE) == "tok-1"
    assert continuation_token({"contents": [item("Storytime01", "x")]}) is None
    assert client_version('"INNERTUBE_CLIENT_VERSION":"2.1.0"') == "2.1.0"
    assert client_version("nothing").startswith("2.")


def public_client(*responses):
    session = FakeSession(*responses)
    from requests.cookies import RequestsCookieJar
    session.cookies = RequestsCookieJar()
    return PublicYouTubeClient(session=session, gap_secs=0, sleep=lambda _s: None), session


def test_playlist_videos_follows_continuations():
    second = {"onResponseReceivedActions": [{"appendContinuationItemsAction": {"continuationItems": [
        item("Storytime02", "Episode 2"), item("Storytime03", "Episode 3"), more("tok-2")]}}]}
    third = {"onResponseReceivedActions": [{"appendContinuationItemsAction": {"continuationItems": [
        item("Storytime04", "Episode 4", "61")]}}]}
    client, session = public_client(page_html(FIRST_PAGE), browse(second), browse(third))
    videos = client.playlist_videos(PLAYLIST)
    assert [v.video_id for v in videos] == ["Storytime01", "Storytime02", "Storytime03", "Storytime04"]
    assert videos[-1] == PlaylistItem("Storytime04", "Episode 4", 61.0)
    assert session.calls[0]["url"] == f"https://www.youtube.com/playlist?list={PLAYLIST}"
    call = session.calls[1]
    assert (call["method"], call["json"]["continuation"]) == ("POST", "tok-1")
    assert call["json"]["context"]["client"]["clientVersion"] == "2.20261020.01.00"
    assert session.calls[2]["json"]["continuation"] == "tok-2"


def test_playlist_videos_keeps_what_it_has_when_a_continuation_fails():
    client, _ = public_client(page_html(FIRST_PAGE), FakeResponse(500))
    assert [v.video_id for v in client.playlist_videos(PLAYLIST)] == ["Storytime01", "Storytime02"]
    client, session = public_client(page_html(FIRST_PAGE), browse({"contents": [more("again")]}))
    assert len(client.playlist_videos(PLAYLIST, max_pages=2)) == 2 and len(session.calls) == 2  # capped
    assert client.playlist_videos("not-a-playlist") == []


def test_resolve_playlist():
    client, session = public_client(page_html(FIRST_PAGE))
    assert client.resolve_playlist(f"https://www.youtube.com/playlist?list={PLAYLIST}") == Playlist(
        PLAYLIST, "Storytime Podcast")
    assert client.resolve_playlist("@SeventySeven") is None and len(session.calls) == 1  # no request


def add_playlist_source(conn):
    from podbridge.linking import link_source
    with conn:
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id, kind) "
                     "VALUES ('YouTube: Storytime Podcast', ?, '', 'youtube')", (PLAYLIST,))
    source_id = conn.execute("SELECT id FROM sources WHERE campaign_id = ?", (PLAYLIST,)).fetchone()[0]
    link_source(conn, source_id, "pc-story")
    return source_id


def test_discovery_reads_playlist_sources_alongside_channels(yt_env):  # noqa: F811
    conn, store = yt_env
    source_id = add_playlist_source(conn)
    playlists = {PLAYLIST: ("Storytime Podcast", [PlaylistItem("Storytime01", "Episode 1", 2400.0),
                                                  PlaylistItem("NewsAgent01", "Crossover", 2490.0)])}
    uploads = {CHANNEL: [PlaylistItem("NewsAgent01", "Is this the end for the PM? | The News Agents", 2490.0)]}
    yt = FakeYouTube([HistoryItem("Storytime01", "Episode 1", 25.0)], DETAILS, uploads=uploads, playlists=playlists)
    results = {r.label: r for r in discover_youtube_all(conn, store, yt)}
    assert results["YouTube: Storytime Podcast"].posts_seen == 2
    rows = {r["patreon_post_id"]: (r["published_at"], r["patreon_position_secs"]) for r in conn.execute(
        "SELECT e.patreon_post_id, e.published_at, p.patreon_position_secs FROM episodes e "
        "LEFT JOIN progress p ON p.episode_id = e.id WHERE e.source_id = ?", (source_id,))}
    assert rows["Storytime01"] == (None, 600.0)  # 25% of 40:00
    # A video on both the channel and the playlist gets its date in both sources from one lookup.
    assert rows["NewsAgent01"][0] == "2026-10-02T13:00:00Z"
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE patreon_post_id = 'NewsAgent01' "
                        "AND published_at IS NOT NULL").fetchone()[0] == 2
    assert yt.detail_calls.count("NewsAgent01") == 1


def test_playlist_only_skips_history_channel_lookups(yt_env):  # noqa: F811
    conn, store = yt_env
    conn.execute("DELETE FROM sources WHERE campaign_id = ?", (CHANNEL,))
    conn.commit()
    add_playlist_source(conn)
    yt = FakeYouTube([HistoryItem("OtherChan03", "Cat video", 30.0)], DETAILS,
                     playlists={PLAYLIST: ("Storytime Podcast", [])})
    discover_youtube_all(conn, store, yt)
    assert yt.detail_calls == []  # no channel sources, so unlisted history videos aren't looked up


def test_add_playlist_on_sources_page(app, authed):
    yt = FakeYouTube([], DETAILS, playlists={PLAYLIST: ("Storytime Podcast", [])})
    use_yt(app, yt)
    use_pc(app, FakePocketCastsClient(podcasts=[Podcast("pc-story", "Storytime", None, "feeds.example.com")]))
    configure_pc(authed)
    url = f"https://www.youtube.com/playlist?list={PLAYLIST}"
    html = post(authed, "/sources/youtube", page="/sources", channel=url).get_data(as_text=True)
    assert "Added YouTube: Storytime Podcast." in html
    assert "YouTube playlist" in html and PLAYLIST in html
    assert "playlist feeds" in authed.get("/sources/2/link").get_data(as_text=True)
    again = post(authed, "/sources/youtube", page="/sources", channel=PLAYLIST).get_data(as_text=True)
    assert "That playlist is already a source." in again
    missing = post(authed, "/sources/youtube", page="/sources",
                   channel="https://www.youtube.com/playlist?list=PLdoesnotexist000").get_data(as_text=True)
    assert "find a YouTube playlist for" in missing
