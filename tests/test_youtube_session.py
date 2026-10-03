"""Keeping one shared YouTube login healthy across client instances."""

from __future__ import annotations

from requests.cookies import create_cookie
from test_youtube import COOKIES, CookieSession, page

from podbridge.youtube import HttpYouTubeClient, parse_cookies, serialise_cookies


class Store:
    """Stands in for the settings store's youtube_cookies secret."""

    def __init__(self, text):
        self.text = text

    def save(self, text):
        self.text = text

    def load(self):
        return self.text


def client_for(store, *responses):
    session = CookieSession(*responses)
    return HttpYouTubeClient(store.load(), store.save, session=session, gap_secs=0, sleep=lambda _s: None,
                             load_cookies=store.load), session


def psidts(session):
    return {c.name: c.value for c in session.cookies}.get("__Secure-1PSIDTS")


def test_second_client_picks_up_a_rotation_saved_by_the_first():
    store = Store(COOKIES)
    first, first_session = client_for(store, page('"LOGGED_IN":true'))
    second, second_session = client_for(store, page('"LOGGED_IN":true'))  # created before the rotation

    first_session.cookies.set_cookie(create_cookie("__Secure-1PSIDTS", "rotated-1", domain=".youtube.com"))
    first.check_session()
    assert "rotated-1" in store.text

    second.check_session()
    assert psidts(second_session) == "rotated-1"  # reloaded instead of using its stale copy
    assert "rotated-1" in store.text              # and didn't overwrite the newer save


def test_no_reload_when_nothing_changed():
    store = Store(COOKIES)
    client, session = client_for(store, page('"LOGGED_IN":true'), page('"LOGGED_IN":true'))
    session.cookies.set_cookie(create_cookie("extra", "kept", domain=".youtube.com"))
    client.check_session()   # saves the jar including 'extra'
    client.check_session()   # store now equals what this client saved: no reload, nothing lost
    assert {c.name for c in session.cookies} >= {"extra", "SAPISID"}


def test_round_trip_preserves_cookies():
    session = CookieSession()
    HttpYouTubeClient(COOKIES, lambda _t: None, session=session)
    text = serialise_cookies(session.cookies)
    assert {(c["name"], c["value"]) for c in parse_cookies(text)} == {
        ("SAPISID", "sapisid-secret-value"), ("__Secure-1PSID", "psid-secret-value")}


def test_settings_show_how_long_the_login_lasted(app, authed):
    from test_web_patreon import post
    from test_youtube import DETAILS, FakeYouTube
    app.extensions["youtube_client_factory"] = lambda _s: FakeYouTube([], DETAILS)
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1", youtube_cookies="SID=x")
    assert "Current login pasted" in authed.get("/settings").get_data(as_text=True)

    app.extensions["youtube_client_factory"] = lambda _s: FakeYouTube([], DETAILS, logged_in=False)
    post(authed, "/settings", page="/settings", action="test_youtube", sync_interval_minutes="15", dry_run="1")
    html = authed.get("/settings").get_data(as_text=True)
    assert "Last login lasted" in html

    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1", youtube_cookies="SID=y")
    assert "Last login lasted" not in authed.get("/settings").get_data(as_text=True)  # new login, new clock
