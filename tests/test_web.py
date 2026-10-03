"""Login, CSRF, Settings page and /healthz."""

from __future__ import annotations

from conftest import csrf_from, login

from podbridge.auth import MAX_FAILURES
from podbridge.views import normalise_patreon_cookie


def test_pages_require_login(client):
    for path in ("/", "/settings"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_healthz_is_public_and_reveals_nothing(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok", "patreon_session_valid": None, "pocketcasts_session_valid": None,
    }


def test_wrong_password_rejected(client):
    assert login(client, "nope").status_code == 401
    assert client.get("/").status_code == 302


def test_login_redirects_to_next_but_not_offsite(client):
    token = csrf_from(client.get("/login"))
    response = client.post("/login?next=//evil.example/x", data={"password": "correct horse battery staple",
                                                                   "csrf_token": token})
    assert response.headers["Location"] == "/"


def test_lockout_after_repeated_failures(client):
    for _ in range(MAX_FAILURES):
        login(client, "wrong")
    assert login(client).status_code == 429


def test_post_without_csrf_is_rejected(authed):
    assert authed.post("/settings", data={"sync_interval_minutes": "15"}).status_code == 400


def test_logout(authed):
    token = csrf_from(authed.get("/"))
    authed.post("/logout", data={"csrf_token": token})
    assert authed.get("/").status_code == 302


def save(client, **fields):
    token = csrf_from(client.get("/settings"))
    data = {"csrf_token": token, "sync_interval_minutes": "15", "dry_run": "1", **fields}
    return client.post("/settings", data=data, follow_redirects=True)


def test_secrets_are_write_only(authed):
    page = save(authed, patreon_session_id="session_id=super-secret-cookie; Path=/",
                pocketcasts_email="me@example.com", pocketcasts_password="pc-password-123")
    html = page.get_data(as_text=True)
    assert "Settings saved." in html
    for secret in ("super-secret-cookie", "me@example.com", "pc-password-123"):
        assert secret not in html
    assert "Set: leave blank to keep" in html
    assert "Not verified" in html  # both services now configured but unverified


def test_blank_secret_keeps_existing_and_clear_removes(authed, app):
    save(authed, patreon_session_id="cookie-one")
    save(authed)  # blank field: unchanged
    with app.app_context():
        from podbridge.settings_store import get_store
        assert get_store().get_secret("patreon_session_id") == "cookie-one"
    save(authed, clear_patreon_session_id="1")
    with app.app_context():
        from podbridge.settings_store import get_store
        assert get_store().get_secret("patreon_session_id") is None


def test_interval_validation_and_dry_run_toggle(authed, app):
    html = save(authed, sync_interval_minutes="1").get_data(as_text=True)
    assert "between 5 and 1440" in html
    token = csrf_from(authed.get("/settings"))
    authed.post("/settings", data={"csrf_token": token, "sync_interval_minutes": "30"})
    with app.app_context():
        from podbridge.settings_store import get_store
        store = get_store()
        assert store.get_int("sync_interval_minutes") == 30
        assert store.get_bool("dry_run") is False


def test_webhook_must_be_http(authed):
    html = save(authed, alert_webhook_url="javascript:alert(1)").get_data(as_text=True)
    assert "http(s) URL" in html


def test_dashboard_renders(authed):
    html = authed.get("/").get_data(as_text=True)
    assert "Dry run is on" in html
    assert "Enabled sources" in html


def test_normalise_patreon_cookie():
    assert normalise_patreon_cookie("abc") == "abc"
    assert normalise_patreon_cookie(" session_id=abc; Path=/ ") == "abc"
    assert normalise_patreon_cookie("SESSION_ID=abc;") == "abc"
