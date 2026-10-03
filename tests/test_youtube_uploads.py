"""YouTube sources list every recent upload, not just watched videos."""

from __future__ import annotations

import pytest
from conftest import fixture_json
from test_youtube import CHANNEL, DETAILS, FakeYouTube, yt_env  # noqa: F401  (yt_env is a fixture)

from podbridge import discovery
from podbridge.discovery import discover_youtube_all
from podbridge.youtube import HistoryItem, PlaylistItem, parse_clock, parse_history, parse_playlist, uploads_playlists

UPLOADS = {CHANNEL: [
    PlaylistItem("NewUpload05", "Brand new episode | The News Agents", 2400.0),
    PlaylistItem("NewsAgent01", "Is this the end for the PM? | The News Agents", 2490.0),
    PlaylistItem("NewsAgent02", "Trump's tariffs explained", 2280.0),
]}


def episodes(conn):
    return {r["patreon_post_id"]: (r["published_at"], r["patreon_position_secs"]) for r in conn.execute(
        "SELECT e.patreon_post_id, e.published_at, p.patreon_position_secs FROM episodes e "
        "LEFT JOIN progress p ON p.episode_id = e.id WHERE e.source_id = 2")}


def test_parse_playlist_and_clock():
    data = {"contents": [
        {"playlistVideoRenderer": {"videoId": "NewsAgent01", "title": {"runs": [{"text": "A | The News Agents"}]},
                                   "lengthSeconds": "2521"}},
        {"playlistVideoRenderer": {"videoId": "NewsAgent02", "title": {"simpleText": "B"},
                                   "lengthText": {"simpleText": "1:03:33"}}},
        {"lockupViewModel": {"contentId": "NewsAgent03", "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                             "metadata": {"lockupMetadataViewModel": {"title": {"content": "C"}}},
                             "contentImage": {"thumbnailViewModel": {"overlays": [
                                 {"thumbnailBadgeViewModel": {"text": "53:09"}}]}}}},
    ]}
    assert parse_playlist(data) == [PlaylistItem("NewsAgent01", "A | The News Agents", 2521.0),
                                    PlaylistItem("NewsAgent02", "B", 3813.0),
                                    PlaylistItem("NewsAgent03", "C", 3189.0)]
    assert parse_clock("53:09") == 3189.0 and parse_clock("LIVE") is None
    assert uploads_playlists("UCVbgFf27--l2zYAneDEgyJA") == ["UULFVbgFf27--l2zYAneDEgyJA", "UUVbgFf27--l2zYAneDEgyJA"]


def test_unwatched_uploads_are_listed_as_not_started(yt_env):  # noqa: F811
    conn, store = yt_env
    history = parse_history(fixture_json("youtube_history.json"))
    yt = FakeYouTube(history, DETAILS, uploads=UPLOADS)
    [result] = discover_youtube_all(conn, store, yt)
    assert (result.posts_seen, result.added) == (3, 3)
    found = episodes(conn)
    assert found["NewUpload05"] == (None, None)                   # not watched: no progress, no date yet
    assert found["NewsAgent01"] == ("2026-10-02T13:00:00Z", 1245.0)  # watched 50%
    # Channel already known from the uploads list, so only the other channel's history video needs a lookup
    # for its channel; the listed ones are looked up for their publish dates.
    assert "OtherChan03" in yt.detail_calls


def test_detail_fetches_are_capped_per_run(yt_env, monkeypatch):  # noqa: F811
    conn, store = yt_env
    monkeypatch.setattr(discovery, "MAX_DETAIL_FETCHES_PER_RUN", 1)
    yt = FakeYouTube([], DETAILS, uploads=UPLOADS)
    discover_youtube_all(conn, store, yt)
    assert len(yt.detail_calls) == 1
    discover_youtube_all(conn, store, yt)
    discover_youtube_all(conn, store, yt)
    assert len(yt.detail_calls) == 3  # one more per run, never refetched
    assert episodes(conn)["NewsAgent02"][0] == "2026-10-01T13:00:00Z"


def test_progress_is_kept_when_a_video_leaves_the_history_page(yt_env):  # noqa: F811
    conn, store = yt_env
    watched = [HistoryItem("NewsAgent01", "x", 50.0)]
    discover_youtube_all(conn, store, FakeYouTube(watched, DETAILS, uploads=UPLOADS))
    updated = conn.execute("SELECT patreon_updated_at FROM progress p JOIN episodes e ON e.id = p.episode_id "
                           "WHERE e.patreon_post_id = 'NewsAgent01'").fetchone()[0]
    discover_youtube_all(conn, store, FakeYouTube([], DETAILS, uploads=UPLOADS))  # scrolled off
    assert episodes(conn)["NewsAgent01"][1] == 1245.0
    assert conn.execute("SELECT patreon_updated_at FROM progress p JOIN episodes e ON e.id = p.episode_id "
                        "WHERE e.patreon_post_id = 'NewsAgent01'").fetchone()[0] == updated


@pytest.mark.parametrize("title", ["Brand new episode | The News Agents"])
def test_new_upload_matches_by_title_before_its_date_is_known(yt_env, title):  # noqa: F811
    from podbridge.linking import refresh_all
    from podbridge.pocketcasts import CatalogueEpisode
    from fakes import FakePocketCastsClient
    conn, store = yt_env
    discover_youtube_all(conn, store, FakeYouTube([], {}, uploads={CHANNEL: [PlaylistItem("NewUpload05", title, 2400.0)]}))
    pc = FakePocketCastsClient(catalogue=[CatalogueEpisode("p-new", "Brand new episode", "2026-10-03T05:00:00Z", 2700.0)])
    [result] = refresh_all(conn, store, pc)
    assert result.newly_matched == 1
