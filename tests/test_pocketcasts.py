"""Pocket Casts client: parsing, auth/token handling and requests, against fakes."""

from __future__ import annotations

import pytest
import requests
from conftest import fixture_json
from fakes import FakeResponse, FakeSession

from podbridge.http import HttpClient
from podbridge.pocketcasts import (
    HttpPocketCastsClient, PocketCastsAuthError, PocketCastsBlocked, PocketCastsError, TokenCache,
    parse_catalogue, parse_podcasts, parse_states,
)

LOGIN_OK = {"accessToken": "acc-1", "refreshToken": "ref-1", "tokenType": "Bearer", "expiresIn": 3600}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_client(api_responses, cache_responses=(), refresh_token=None, clock=None):
    api_session = FakeSession(*api_responses)
    cache_sessions = [FakeSession(*cache_responses), FakeSession(*cache_responses)]
    saved: list[str] = []
    tokens = TokenCache(clock=clock or Clock())
    no_sleep = {"gap_secs": 0, "sleep": lambda _s: None}
    client = HttpPocketCastsClient(
        "me@example.com", "pw", refresh_token, saved.append, tokens,
        api=HttpClient("https://api.pocketcasts.com", {}, session=api_session, **no_sleep),
        caches=[HttpClient(f"https://cache{i}", {}, session=s, **no_sleep) for i, s in enumerate(cache_sessions)],
    )
    return client, api_session, cache_sessions, saved, tokens


def test_parse_podcasts_keeps_only_feed_host():
    podcasts = parse_podcasts(fixture_json("pocketcasts_podcast_list.json"))
    assert [(p.uuid, p.title, p.feed_host) for p in podcasts] == [
        ("pc-podcast-public", "Some Other Show", "feeds.example.com"),
        ("pc-podcast-bb", "Button Boys", "www.patreon.com"),
    ]
    assert "FIXTURE" not in repr(podcasts), "private feed URL or token leaked"


def test_parse_catalogue_and_states():
    episodes, has_more = parse_catalogue(fixture_json("pocketcasts_catalogue.json"))
    assert has_more is False
    assert episodes[0].uuid == "pc-ep-peep"
    assert episodes[0].duration_secs == 2838.0
    assert episodes[0].published_at == "2026-09-30T23:02:03Z"
    assert "FIXTURE" not in repr(episodes)
    states = parse_states(fixture_json("pocketcasts_states.json"))
    assert states["pc-ep-peep"].status == 2 and states["pc-ep-peep"].played_up_to == 1200
    assert states["pc-ep-dad"].status == 3


def test_login_then_authenticated_call_reuses_token():
    client, api, _, saved, _ = make_client([
        FakeResponse(200, LOGIN_OK),
        FakeResponse(200, fixture_json("pocketcasts_podcast_list.json")),
        FakeResponse(200, fixture_json("pocketcasts_states.json")),
    ])
    assert len(client.list_podcasts()) == 2
    client.episode_states("pc-podcast-bb")
    assert [c["url"] for c in api.calls] == [
        "https://api.pocketcasts.com/user/login_pocket_casts",
        "https://api.pocketcasts.com/user/podcast/list",
        "https://api.pocketcasts.com/user/podcast/episodes",
    ]
    assert api.calls[0]["json"] == {"email": "me@example.com", "password": "pw", "scope": "mobile"}
    assert api.calls[1]["headers"]["Authorization"] == "Bearer acc-1"
    assert api.calls[2]["json"] == {"uuid": "pc-podcast-bb"}
    assert saved == ["ref-1"]


def test_expired_access_token_uses_refresh_token():
    clock = Clock()
    client, api, _, saved, tokens = make_client([
        FakeResponse(200, {"accessToken": "acc-2", "refreshToken": "ref-2", "expiresIn": 3600}),
        FakeResponse(200, {"podcasts": []}),
    ], refresh_token="ref-1", clock=clock)
    tokens.set("old", 60)  # inside the safety margin, so treated as expired
    client.list_podcasts()
    assert api.calls[0]["url"].endswith("/user/token")
    assert api.calls[0]["json"] == {"grant_type": "refresh_token", "refresh_token": "ref-1"}
    assert saved == ["ref-2"]


def test_failed_refresh_falls_back_to_password_login():
    client, api, _, _, _ = make_client([
        FakeResponse(401, {}), FakeResponse(200, LOGIN_OK), FakeResponse(200, {"podcasts": []}),
    ], refresh_token="stale")
    client.list_podcasts()
    assert [c["url"].rsplit("/", 2)[-1] for c in api.calls] == ["token", "login_pocket_casts", "list"]


def test_401_on_call_gets_new_token_and_retries_once():
    client, api, _, _, tokens = make_client([
        FakeResponse(401, {}), FakeResponse(200, LOGIN_OK), FakeResponse(200, {"podcasts": []}),
    ])
    tokens.set("revoked", 3600)
    assert client.list_podcasts() == []
    assert len(api.calls) == 3


def test_bad_credentials_and_blocks():
    client, *_ = make_client([FakeResponse(401, {"errorMessage": "x"})])
    with pytest.raises(PocketCastsAuthError):
        client.check_login()
    client, *_ = make_client([FakeResponse(429)])
    with pytest.raises(PocketCastsBlocked):
        client.check_login()


def test_catalogue_falls_back_to_second_host():
    client, _, caches, _, _ = make_client(
        [], cache_responses=[requests.ConnectionError("dns")] * 3 + [FakeResponse(200, fixture_json("pocketcasts_catalogue.json"))])
    caches[1].responses = [FakeResponse(200, fixture_json("pocketcasts_catalogue.json"))]
    episodes, _ = client.list_episodes("pc-podcast-bb")
    assert len(episodes) == 5
    assert caches[0].calls[0]["url"] == "https://cache0/mobile/podcast/full/pc-podcast-bb"
    assert "Authorization" not in (caches[0].calls[0]["headers"] or {})


def test_catalogue_failure_everywhere():
    client, _, caches, _, _ = make_client([], cache_responses=[FakeResponse(200, {"podcast": {}})])
    with pytest.raises(PocketCastsError, match="catalogue"):
        client.list_episodes("x")


def test_update_episode_sends_all_five_fields_as_ints():
    client, api, _, _, tokens = make_client([FakeResponse(200, {})])
    tokens.set("acc", 3600)
    client.update_episode("ep", "pod", position=381.98, duration=2838.4, status=2)
    assert api.calls[0]["url"].endswith("/sync/update_episode")
    assert api.calls[0]["json"] == {"uuid": "ep", "podcast": "pod", "position": 381, "duration": 2838, "status": 2}


def test_set_starred_uses_the_web_players_star_call():
    client, api, _, _, tokens = make_client([FakeResponse(200, {}), FakeResponse(200, {})])
    tokens.set("acc", 3600)
    client.set_starred("ep", "pod", True)
    client.set_starred("ep", "pod", False)
    assert api.calls[0]["url"].endswith("/sync/update_episode_star")
    assert [c["json"] for c in api.calls] == [{"uuid": "ep", "podcast": "pod", "star": True},
                                              {"uuid": "ep", "podcast": "pod", "star": False}]
