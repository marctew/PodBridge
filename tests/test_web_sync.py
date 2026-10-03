"""Sync now, the countdown reset, and the Activity page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_linking import fake_pocketcasts
from test_web_patreon import post, use_client
from test_web_pocketcasts import configure_pc, use_pc

from podbridge import create_app
from podbridge.scheduler import JOB_ID


def setup_sync(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    use_pc(app, fake_pocketcasts())
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1",
         patreon_session_id="cookie")
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb")


def test_sync_now_runs_and_shows_in_activity(app, authed):
    setup_sync(app, authed)
    html = post(authed, "/sync").get_data(as_text=True)
    assert "Sync (dry run) done: 2 checked, 0 written to Pocket Casts" in html
    activity = authed.get("/activity").get_data(as_text=True)
    assert "manual · dry run" in activity
    assert "Skipped: not ahead" in activity
    assert "Hidden Cache - Try Not to Peep" in activity


def test_sync_now_without_credentials(authed):
    html = post(authed, "/sync").get_data(as_text=True)
    assert "Pocket Casts email and password are not set" in html


def test_missing_patreon_login_only_fails_patreon(app, authed):
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)  # Pocket Casts set up, Patreon not
    html = post(authed, "/sync").get_data(as_text=True)
    assert "Sync error: Patreon: Patreon session cookie is not set" in html


@pytest.fixture
def scheduled_app(config):
    from dataclasses import replace
    app = create_app(replace(config, scheduler_enabled=True))
    app.config["TESTING"] = True
    yield app
    app.extensions["scheduler"].shutdown(wait=False)


def test_manual_sync_restarts_countdown(scheduled_app):
    from conftest import login
    client = scheduled_app.test_client()
    login(client)
    scheduler = scheduled_app.extensions["scheduler"]
    job = scheduler.get_job(JOB_ID)
    job.modify(next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))  # nearly due

    use_client(scheduled_app, FakePatreonClient(fixture_posts()))
    use_pc(scheduled_app, fake_pocketcasts())
    post(client, "/settings", page="/settings", sync_interval_minutes="15", patreon_session_id="c",
         pocketcasts_email="me@example.com", pocketcasts_password="pw-123456")
    post(client, "/sync")

    remaining = scheduler.get_job(JOB_ID).next_run_time - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)
    assert "Next run" in client.get("/").get_data(as_text=True)


def test_interval_change_reschedules(scheduled_app):
    from conftest import login
    client = scheduled_app.test_client()
    login(client)
    post(client, "/settings", page="/settings", sync_interval_minutes="30")
    remaining = scheduled_app.extensions["scheduler"].get_job(JOB_ID).next_run_time - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)
