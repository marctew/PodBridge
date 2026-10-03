"""Patreon-backed pages: Save & test, refresh, Episodes and Sources."""

from __future__ import annotations

from conftest import csrf_from
from fakes import FakePatreonClient
from test_discovery import fixture_posts

from podbridge.http import TransportError
from podbridge.patreon import Collection, PatreonBlocked


def use_client(app, client) -> None:
    app.extensions["patreon_client_factory"] = lambda _store: client


def post(authed, path, page="/", **data):
    token = csrf_from(authed.get(page))
    return authed.post(path, data={"csrf_token": token, **data}, follow_redirects=True)


def test_save_and_test_reports_valid_session(app, authed):
    use_client(app, FakePatreonClient())
    html = post(authed, "/settings", page="/settings", action="test_patreon", sync_interval_minutes="15",
                patreon_session_id="abc123").get_data(as_text=True)
    assert "Patreon session is valid." in html
    assert "badge-ok" in html
    assert authed.get("/healthz").get_json()["patreon_session_valid"] is True


def test_save_and_test_reports_expired_session(app, authed):
    use_client(app, FakePatreonClient(logged_in=False))
    html = post(authed, "/settings", page="/settings", action="test_patreon", sync_interval_minutes="15",
                patreon_session_id="stale-cookie").get_data(as_text=True)
    assert "not logged in" in html
    assert authed.get("/healthz").get_json()["patreon_session_valid"] is False


def test_test_without_cookie_uses_real_factory_and_says_not_set(authed):
    html = post(authed, "/settings", page="/settings", action="test_patreon",
                sync_interval_minutes="15").get_data(as_text=True)
    assert "Patreon session cookie is not set" in html


def test_refresh_populates_episodes_page(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    html = post(authed, "/episodes/refresh").get_data(as_text=True)
    assert "4 posts, 3 new, 1 without media skipped" in html
    assert "Hidden Cache - Try Not to Peep" in html
    assert "6:21" in html  # 381.98 s in progress
    assert "Watched" in html

    in_progress = authed.get("/episodes?filter=in_progress").get_data(as_text=True)
    assert "Try Not to Peep" in in_progress
    assert "Pierre" not in in_progress


def test_refresh_redirect_ignores_offsite_next(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    token = csrf_from(authed.get("/"))
    response = authed.post("/episodes/refresh", data={"csrf_token": token, "next": "//evil.example"})
    assert response.headers["Location"] == "/"


def test_refresh_failures_are_reported(app, authed):
    use_client(app, FakePatreonClient(logged_in=False))
    assert "session has expired" in post(authed, "/episodes/refresh").get_data(as_text=True)
    use_client(app, FakePatreonClient(error=PatreonBlocked("Patreon returned HTTP 429")))
    assert "pushing back" in post(authed, "/episodes/refresh").get_data(as_text=True)
    use_client(app, FakePatreonClient(error=TransportError("ConnectionError after 3 attempts")))
    assert "Couldn&#39;t reach Patreon" in post(authed, "/episodes/refresh").get_data(as_text=True)


def test_sources_lists_collections_and_adds_source(app, authed):
    use_client(app, FakePatreonClient(collections=[Collection("1968959", "Player 4", 7)]))
    html = authed.get("/sources?campaign_id=14434926").get_data(as_text=True)
    assert "Player 4 (7 posts)" in html
    html = post(authed, "/sources", page="/sources", campaign_id="14434926", collection_id="1968959",
                label="").get_data(as_text=True)
    assert "Added Campaign 14434926 / collection 1968959." in html
    html = post(authed, "/sources", page="/sources", campaign_id="14434926",
                collection_id="1968959").get_data(as_text=True)
    assert "already a source" in html


def test_sources_rejects_non_numeric_ids(authed):
    html = post(authed, "/sources", page="/sources", campaign_id="abc", collection_id="1").get_data(as_text=True)
    assert "must be numbers" in html


def test_toggle_source(app, authed):
    html = post(authed, "/sources/1/toggle", page="/sources").get_data(as_text=True)
    assert ">Enable<" in html
