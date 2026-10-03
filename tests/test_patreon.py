"""Patreon client parsing and HTTP behaviour, against recorded-shape fixtures."""

from __future__ import annotations

import pytest
import requests
from conftest import fixture_json
from fakes import FakeResponse, FakeSession

from podbridge.http import HttpClient, TransportError
from podbridge.patreon import (
    HttpPatreonClient, PatreonBlocked, PatreonError, PatreonSessionExpired, parse_collections,
    parse_posts_page, parse_progress,
)


def client_with(*responses) -> tuple[HttpPatreonClient, FakeSession]:
    session = FakeSession(*responses)
    http = HttpClient("https://www.patreon.com", {"Cookie": "session_id=x"}, gap_secs=0,
                      session=session, sleep=lambda _s: None)
    return HttpPatreonClient("x", http=http), session


def test_parse_posts_keeps_only_needed_fields():
    posts, cursor = parse_posts_page(fixture_json("patreon_posts_page1.json"))
    assert cursor == "FIXTURE_CURSOR_2"
    first, second = posts
    assert first.post_id == "171048709"
    assert first.media_id == "755348961"
    assert first.duration_secs == pytest.approx(2837.84)
    assert first.progress.position_secs == pytest.approx(381.98)
    assert first.progress.watch_state == "is_watching"
    assert first.progress.is_watched is False
    assert first.url == "https://www.patreon.com/posts/hidden-cache-try-171048709"
    assert "FIXTURE_" not in repr(posts), "signed URLs or tokens leaked out of the parser"
    assert second.title == "Hidden Cache - Where's Pierre's Dad?"
    assert second.url == "https://www.patreon.com/posts/hidden-cache-where-170000002"
    assert second.progress.is_watched is True


def test_parse_posts_handles_unplayed_text_and_foreign_urls():
    posts, cursor = parse_posts_page(fixture_json("patreon_posts_page2.json"))
    assert cursor is None
    audio, text = posts
    assert audio.progress.position_secs is None and audio.progress.updated_at is None
    assert text.media_id is None
    assert text.url is None  # not a patreon.com URL


def test_logged_out_progress_shape_has_no_position():
    progress = parse_progress({"is_watched": False, "watch_state": "is_not_watched"})
    assert progress.position_secs is None
    assert progress.updated_at is None


@pytest.mark.parametrize("junk", [None, [], "x", {"data": "nope"}, {"data": [1, {"type": "media"}]}])
def test_parse_posts_tolerates_junk(junk):
    assert parse_posts_page(junk) == ([], None)


def test_parse_collections():
    collections = parse_collections(fixture_json("patreon_collections.json"))
    assert [(c.collection_id, c.title, c.num_posts) for c in collections] == [
        ("1909234", "Hidden Cache", 63), ("1968959", "Player 4", 7), ("9", "Collection 9", None),
    ]


def test_list_posts_follows_pagination():
    client, session = client_with(
        FakeResponse(200, fixture_json("patreon_posts_page1.json")),
        FakeResponse(200, fixture_json("patreon_posts_page2.json")),
    )
    posts = client.list_posts("14434926", "1909234")
    assert [p.post_id for p in posts] == ["171048709", "170000002", "170000003", "170000004"]
    assert "page[cursor]" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["page[cursor]"] == "FIXTURE_CURSOR_2"
    assert "post_file" in session.calls[0]["params"]["fields[post]"]
    assert session.calls[0]["params"]["filter[collection_id]"] == "1909234"


def test_check_session_states():
    client, _ = client_with(FakeResponse(200, {"data": {"id": "1", "type": "user"}}))
    assert client.check_session() is True
    client, _ = client_with(FakeResponse(401, {"errors": [{}]}))
    assert client.check_session() is False
    client, _ = client_with(FakeResponse(200, {"data": None}))
    assert client.check_session() is False
    client, _ = client_with(FakeResponse(500))
    with pytest.raises(PatreonError):
        client.check_session()


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_statuses_raise(status):
    client, _ = client_with(FakeResponse(status))
    with pytest.raises(PatreonBlocked):
        client.list_posts("1", "2")


def test_session_rejected_mid_listing():
    client, _ = client_with(FakeResponse(401))
    with pytest.raises(PatreonSessionExpired):
        client.list_posts("1", "2")


def test_network_errors_retry_then_fail_cleanly():
    client, session = client_with(requests.ConnectionError("dns"), FakeResponse(200, {"data": {"id": "1", "type": "user"}}))
    assert client.check_session() is True
    assert len(session.calls) == 2

    client, _ = client_with(*[requests.ConnectionError("https://www.patreon.com/api?secret")] * 3)
    with pytest.raises(TransportError) as excinfo:
        client.check_session()
    assert "patreon.com" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
