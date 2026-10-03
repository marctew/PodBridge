"""Resume position, link building (spec 9a) and the /go redirects."""

from __future__ import annotations

from dataclasses import replace

from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_linking import fake_pocketcasts
from test_web_patreon import post
from test_web_sync import setup_sync

from podbridge.patreon import Progress
from podbridge.pocketcasts import EpisodeState
from podbridge.resume import Resume, build_resume_url, last_touched, resume_position

URL = "https://www.patreon.com/posts/hidden-cache-try-171048709"


def test_resume_takes_the_further_position():
    assert resume_position(381.98, False, 2838, 2, 1200.0) == Resume(1200.0, "pocketcasts")
    assert resume_position(2444.5, True, 2838, 2, 1200.0) == Resume(2444.5, "patreon")  # early watched flag
    assert resume_position(None, False, 2838, None, None) == Resume(0.0, "start")


def test_resume_is_none_when_either_side_played():
    assert resume_position(381.98, False, 2838, 3, 2838.0) is None
    assert resume_position(2800.0, True, 2838, 2, 100.0) is None  # within the last minute on Patreon


def test_build_resume_url():
    assert build_resume_url(URL, 2444.9) == URL + "?t=2444"
    assert build_resume_url(URL + "?utm=x&t=5", 60, "t") == URL + "?utm=x&t=60"
    assert build_resume_url(URL, 90, "start") == URL + "?start=90"
    assert build_resume_url(URL + "?t=5", 0) == URL  # start from the beginning


def test_last_touched_picks_newest():
    assert last_touched("2026-10-03T12:21:00.628+00:00", "2026-10-03T13:00:00Z").hour == 13
    assert last_touched(None, None) is None


def setup_with_progress(app, authed, peep_progress, states=None):
    posts = [replace(p, progress=peep_progress) if p.post_id == "171048709" else p for p in fixture_posts()]
    setup_sync(app, authed)
    app.extensions["patreon_client_factory"] = lambda _s: FakePatreonClient(posts)
    pc = fake_pocketcasts()
    if states is not None:
        pc.states = states
    app.extensions["pocketcasts_client_factory"] = lambda _s: pc
    post(authed, "/episodes/refresh")
    return pc


def test_go_uses_fresh_pocketcasts_state(app, authed):
    pc = setup_with_progress(app, authed, Progress(381.98, False, "is_watching", "2026-10-03T12:00:00+00:00"))
    pc.states["pc-ep-peep"] = EpisodeState("pc-ep-peep", 2, 1900.0, 2838.0)  # listened on since the last sync
    response = authed.get("/go/1")
    assert response.status_code == 302
    assert response.headers["Location"] == URL + "?t=1900"


def test_go_falls_back_to_cached_state_when_pocketcasts_fails(app, authed):
    from podbridge.pocketcasts import PocketCastsBlocked
    pc = setup_with_progress(app, authed, Progress(381.98, False, "is_watching", "2026-10-03T12:00:00+00:00"))
    pc.error = PocketCastsBlocked("429")
    assert authed.get("/go/1").headers["Location"] == URL + "?t=1200"


def test_go_latest_and_dashboard_list(app, authed):
    setup_with_progress(app, authed, Progress(2444.5, True, "is_watched", "2026-10-03T12:00:00+00:00"))
    html = authed.get("/").get_data(as_text=True)
    assert "Continue watching" in html
    assert "40:44 / 47:17" in html and "Patreon is ahead" in html
    assert authed.get("/go/latest").headers["Location"] == URL + "?t=2444"


def test_go_requires_login(client):
    response = client.get("/go/latest")
    assert response.status_code == 302 and "/login?next=/go/latest" in response.headers["Location"]


def test_timestamp_param_setting(app, authed):
    setup_with_progress(app, authed, Progress(381.98, False, "is_watching", "2026-10-03T12:00:00+00:00"))
    html = post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1",
                patreon_timestamp_param="bad param!").get_data(as_text=True)
    assert "must be a short name" in html
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1",
         patreon_timestamp_param="start")
    assert authed.get("/go/1").headers["Location"] == URL + "?start=1200"


def test_pocketcasts_change_is_timestamped(app, authed):
    from podbridge.db import get_db
    setup_with_progress(app, authed, Progress(381.98, False, "is_watching", "2026-10-03T12:00:00+00:00"))
    with app.app_context():
        assert get_db().execute("SELECT pocketcasts_changed_at FROM progress WHERE episode_id = 1").fetchone()[0] is None
    pc = fake_pocketcasts()
    pc.states["pc-ep-peep"] = EpisodeState("pc-ep-peep", 2, 1900.0, 2838.0)
    app.extensions["pocketcasts_client_factory"] = lambda _s: pc
    post(authed, "/episodes/refresh")
    with app.app_context():
        assert get_db().execute("SELECT pocketcasts_changed_at FROM progress WHERE episode_id = 1").fetchone()[0]
