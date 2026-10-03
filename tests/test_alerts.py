"""The problem banner (spec section 9, banner only)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from podbridge.alerts import current_problems
from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.settings_store import SettingsStore


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "a.db")
    migrate(conn)
    return conn, SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))


def add_runs(conn, *statuses, error="PatreonBlocked: Patreon returned HTTP 429"):
    with conn:
        for status in statuses:
            conn.execute("INSERT INTO sync_runs (started_at, tier, status, error) VALUES "
                         "('2026-10-03T12:00:00Z', 'full', ?, ?)", (status, None if status == "ok" else error))


def keys(conn, store):
    return [p.key for p in current_problems(conn, store)]


def test_healthy_or_unconfigured_shows_nothing(env):
    conn, store = env
    assert keys(conn, store) == []
    store.set("patreon_status", "expired")  # no cookie set: not a login problem
    assert keys(conn, store) == []


def test_expired_and_failing_logins(env):
    conn, store = env
    store.set_secret("patreon_session_id", "c")
    store.set("patreon_status", "expired")
    store.set("patreon_verified_at", "2026-10-03T10:00:00Z")
    store.set_secret("pocketcasts_email", "e")
    store.set_secret("pocketcasts_password", "p")
    store.set("pocketcasts_status", "error")
    problems = current_problems(conn, store)
    assert [p.key for p in problems] == ["login:patreon", "login:pocketcasts"]
    assert problems[0].target == "settings#patreon" and problems[0].since == "2026-10-03T10:00:00Z"
    store.set("patreon_status", "ok")
    assert keys(conn, store) == ["login:pocketcasts"]  # fixed: banner gone


def test_youtube_uses_expiry_time(env):
    conn, store = env
    store.set_secret("youtube_cookies", "SID=x")
    store.set("youtube_status", "expired")
    store.set("youtube_expired_at", "2026-10-03T09:00:00Z")
    store.set("youtube_verified_at", "2026-10-03T11:00:00Z")
    [problem] = current_problems(conn, store)
    assert problem.since == "2026-10-03T09:00:00Z" and "YouTube" in problem.text


def test_three_failed_syncs(env):
    conn, store = env
    add_runs(conn, "error", "error")
    assert keys(conn, store) == []  # only two
    add_runs(conn, "error")
    [problem] = current_problems(conn, store)
    assert problem.key == "sync" and "HTTP 429" in problem.text and problem.target == "activity"
    add_runs(conn, "ok")
    assert keys(conn, store) == []  # a good run clears it


def test_login_only_aborts_do_not_double_up(env):
    conn, store = env
    add_runs(conn, "aborted", "aborted", "aborted", error="Patreon session expired")
    assert keys(conn, store) == []  # covered by the login banner, not repeated


def test_banner_on_every_page(app, authed):
    from test_web_patreon import post
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1", patreon_session_id="c")
    with app.app_context():
        from podbridge.settings_store import get_store
        get_store().set("patreon_status", "expired")
    for page in ("/", "/library", "/activity", "/sources"):
        html = authed.get(page).get_data(as_text=True)
        assert "Patreon session has expired" in html, page
        assert 'href="/settings#patreon"' in html


def test_no_banner_when_logged_out(client):
    assert 'class="problems"' not in client.get("/login").get_data(as_text=True)
