"""The outside API (Design Uniformity API-STANDARD.md): tokens, the gate, status, feeds, images,
actions, events, webhooks, the display page and the Settings cards."""

from __future__ import annotations

import io
import json
import re

import pytest
from conftest import csrf_from
from test_library import setup_library
from test_web_patreon import post

from podbridge import du_api
from podbridge.api import events, webhooks
from podbridge.db import connect, get_db, migrate, migrations
from podbridge.settings_store import get_store

PEEP = "Hidden Cache - Try Not to Peep"


def make_token(authed, name="Desk screen", scope="read") -> str:
    html = post(authed, "/settings/api/tokens", page="/settings", name=name, scope=scope).get_data(as_text=True)
    match = re.search(r'value="(pb_[^"]+)"', html)
    assert match, "the new token is shown once on the page"
    return match.group(1)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def lib(app, authed):
    setup_library(app, authed)
    return authed


@pytest.fixture
def anon(app):
    return app.test_client()


# --- tokens and the gate ---

def test_tokens_gate_every_api_route(app, authed, anon):
    read = make_token(authed)
    write = make_token(authed, "Home Assistant", "write")

    r = anon.get("/api/v1/status")
    assert r.status_code == 401 and r.get_json()["code"] == "unauthorised"        # JSON, not the login page
    assert anon.get("/api/v1/status", headers=bearer("pb_nope")).status_code == 401
    assert anon.get("/api/v1/status", headers=bearer(read)).status_code == 200
    assert anon.get(f"/api/v1/status?token={read}").status_code == 200            # read tokens may go in the URL
    r = anon.get(f"/api/v1/status?token={write}")
    assert r.status_code == 400 and "header" in r.get_json()["error"]

    r = anon.post("/api/v1/actions/list_add", json={"episode_id": 1}, headers=bearer(read))
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"            # read-only

    assert authed.get("/api/v1/status").status_code == 200                        # the signed-in session reads
    # A session's writes need X-CSRF-Token, not the form field.
    assert authed.post("/api/v1/actions/list_add", json={"episode_id": 1}).status_code == 400

    token_id = get_db_rows(app, "SELECT id FROM api_tokens WHERE name = 'Desk screen'")[0][0]
    post(authed, f"/settings/api/tokens/{token_id}/revoke", page="/settings")
    assert anon.get("/api/v1/status", headers=bearer(read)).status_code == 401
    assert "Desk screen" not in authed.get("/settings").get_data(as_text=True)


def get_db_rows(app, sql, *args):
    with app.app_context():
        return [tuple(r) for r in get_db().execute(sql, args).fetchall()]


def test_meta_lists_what_podbridge_offers(app, authed):
    meta = authed.get("/api/v1/meta").get_json()
    assert meta["app"] == "podbridge" and meta["api"] == 1 and meta["accent"] == "violet"
    assert [f["name"] for f in meta["feeds"]] == ["continue", "new", "up_next", "my_list", "history", "syncs"]
    actions = {a["name"]: a for a in meta["actions"]}
    assert set(actions) == {"sync_now", "mark_played", "mark_unplayed", "list_add", "list_remove"}
    assert actions["sync_now"]["limit"] == "6/hour" and actions["list_add"]["body"] == {"episode_id": "int"}
    assert meta["displays"][0]["name"] == "continue-watching"
    assert meta["events"] == events.TYPES


# --- status and feeds ---

def test_status_and_etag(lib):
    r = lib.get("/api/v1/status")
    body = r.get_json()
    assert body["app"] == "podbridge" and body["state"] in ("ok", "warning")
    assert body["headline"].startswith("1 in progress")
    counts = {c["key"]: c["value"] for c in body["counts"]}
    assert counts["in_progress"] == 1 and set(counts) == {"in_progress", "new_this_week", "my_list", "unmatched",
                                                         "written_today"}
    assert body["generated_at"].endswith("Z") and body["refresh_s"] == 60
    assert r.headers["Cache-Control"] == "private, max-age=30" and r.headers["ETag"]


def test_status_when_not_set_up(authed):
    assert authed.get("/api/v1/status").get_json()["state"] == "off"


def test_continue_feed_item_shape(lib):
    body = lib.get("/api/v1/feeds/continue").get_json()
    assert body["feed"] == "continue" and body["next_cursor"] is None
    [item] = body["items"]
    assert item["title"] == PEEP and item["id"].startswith("episode:")
    assert item["subtitle"].startswith("Button Boys · ") and item["subtitle"].endswith("left")
    assert item["image"] == f"/api/v1/images/episode/{item['meta']['episode_id']}"
    assert item["url"].endswith(f"/episodes/{item['meta']['episode_id']}")
    assert item["at"].endswith("Z") and item["at_label"]
    meta = item["meta"]
    assert meta["state"] == "in_progress" and meta["position_secs"] == 1200.0 and meta["source"] == "patreon"
    assert meta["resume_url"] and meta["matched"] is True


def test_timed_feeds_page_with_cursor(lib):
    first = lib.get("/api/v1/feeds/new?limit=1").get_json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = lib.get(f"/api/v1/feeds/new?limit=1&cursor={first['next_cursor']}").get_json()
    assert second["items"][0]["at"] < first["items"][0]["at"]
    later = lib.get(f"/api/v1/feeds/new?since={first['items'][0]['at']}").get_json()
    assert later["items"] == []
    assert lib.get("/api/v1/feeds/new?since=yesterday").status_code == 400
    assert lib.get("/api/v1/feeds/new?limit=lots").status_code == 400
    r = lib.get("/api/v1/feeds/nope")
    assert r.status_code == 404 and "continue" in r.get_json()["error"]
    assert lib.get("/api/v1/feeds/history").get_json()["items"]          # discovery logged the progress
    assert lib.get("/api/v1/feeds/up_next").status_code == 200


# --- actions ---

def test_list_actions_with_idempotency_and_the_action_log(app, lib, anon):
    write = make_token(lib, "Desk panel", "write")
    episode_id = lib.get("/api/v1/feeds/continue").get_json()["items"][0]["meta"]["episode_id"]
    headers = {**bearer(write), "Idempotency-Key": "abc-1"}
    r = anon.post("/api/v1/actions/list_add", json={"episode_id": episode_id}, headers=headers)
    assert r.status_code == 200 and r.get_json()["message"] == f"Added {PEEP} to My List."
    again = anon.post("/api/v1/actions/list_add", json={"episode_id": episode_id}, headers=headers)
    assert again.get_json() == r.get_json()                                       # the first answer, nothing redone
    assert len(get_db_rows(app, "SELECT * FROM api_audit WHERE action = 'list_add'")) == 1
    assert [i["title"] for i in anon.get("/api/v1/feeds/my_list", headers=bearer(write)).get_json()["items"]] == [PEEP]

    r = anon.post("/api/v1/actions/list_remove", json={"episode_id": episode_id}, headers=bearer(write))
    assert r.get_json()["result"]["changed"] is True
    assert anon.post("/api/v1/actions/list_add", json={"episode_id": 99999}, headers=bearer(write)).status_code == 404
    assert anon.post("/api/v1/actions/list_add", json={"episode_id": "x"}, headers=bearer(write)).status_code == 400
    r = anon.post("/api/v1/actions/delete_everything", json={}, headers=bearer(write))
    assert r.status_code == 404 and "sync_now" in r.get_json()["error"]

    settings = lib.get("/settings").get_data(as_text=True)
    assert "Desk panel" in settings and "Added Hidden Cache" in settings          # the action log on the card


def test_session_action_with_csrf_header(lib):
    episode_id = lib.get("/api/v1/feeds/continue").get_json()["items"][0]["meta"]["episode_id"]
    token = csrf_from(lib.get("/settings"))
    r = lib.post("/api/v1/actions/list_add", json={"episode_id": episode_id}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    assert get_db_rows(lib.application, "SELECT token_name FROM api_audit") == [("Admin session",)]


def test_mark_played_writes_to_pocket_casts(app, lib, anon):
    write = make_token(lib, "HA", "write")
    episode_id = lib.get("/api/v1/feeds/continue").get_json()["items"][0]["meta"]["episode_id"]
    r = anon.post("/api/v1/actions/mark_played", json={"episode_id": episode_id}, headers=bearer(write))
    assert r.status_code == 200 and r.get_json()["result"] == {"episode_id": episode_id, "played": True}
    pc = app.extensions["pocketcasts_client_factory"](None)
    assert pc.updates[-1][4] == 3                                                # played


def test_sync_now_runs_and_is_limited(app, lib, anon):
    write = make_token(lib, "Button", "write")
    for _ in range(6):
        r = anon.post("/api/v1/actions/sync_now", json={}, headers=bearer(write))
        assert r.status_code == 200, r.get_json()
    r = anon.post("/api/v1/actions/sync_now", json={}, headers=bearer(write))
    assert r.status_code == 429 and r.headers["Retry-After"] and "next one can go" in r.get_json()["error"]
    types = [t for (t,) in get_db_rows(app, "SELECT type FROM events")]
    assert "sync.finished" in types


# --- events and webhooks ---

def test_sync_publishes_events_and_announces_new_episodes_once(app, lib):
    with app.app_context():
        conn = get_db()
        assert events.announce_new_episodes(conn) == 0       # setup's syncs already set the mark
        conn.execute("UPDATE settings SET value = '0' WHERE key = ?", (events.NEW_MARK_KEY,))
        conn.execute("UPDATE episodes SET published_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day')")
        conn.commit()
        n = events.announce_new_episodes(conn)
        assert n >= 1 and events.announce_new_episodes(conn) == 0
        new = [e for e in events.after(conn, 0) if e["type"] == "episode.new"]
        assert len(new) == n and new[0]["item"]["image"].startswith("/api/v1/images/episode/")


def test_login_expired_event_only_on_the_change(app, authed):
    with app.app_context():
        store = get_store()
        store.set("patreon_status", "expired")
        store.set("patreon_status", "expired")
        got = [e for e in events.after(get_db(), 0) if e["type"] == "login.expired"]
        assert len(got) == 1 and got[0]["item"]["meta"]["service"] == "patreon"


def test_events_stream_answers_sse(app, authed):
    with app.app_context():
        events.publish(get_db(), events.SYNC_FINISHED, "Synced", {"id": "sync:1", "title": "Synced"})
    r = authed.get("/api/v1/events?last_event_id=0", buffered=False)
    assert r.status_code == 200 and r.mimetype == "text/event-stream"
    chunks = iter(r.response)
    assert next(chunks).startswith(b"retry:")
    first = next(chunks).decode()
    assert first.startswith("event: sync.finished\nid: 1\n") and '"title":"Synced"' in first
    r.close()


def test_webhooks_card_and_signed_delivery(app, authed):
    html = post(authed, "/settings/webhooks", page="/settings", name="Home Assistant",
                url="http://ha.lan:8123/api/webhook/secret-id", event_types="sync.finished").get_data(as_text=True)
    secret = re.search(r'Signing secret for Home Assistant.*?value="([^"]+)"', html, re.S).group(1)
    assert "secret-id" not in authed.get("/settings").get_data(as_text=True)     # the URL only ever masked
    sent = []
    with app.app_context():
        conn = get_db()
        events.publish(conn, events.SYNC_FAILED, "Sync failed", {"id": "sync:2", "title": "x"})   # not wanted
        events.publish(conn, events.SYNC_FINISHED, "Synced", {"id": "sync:3", "title": "Synced"})
        assert webhooks.deliver_due(conn, post=lambda url, body, headers: sent.append((url, body, headers))) == 1
        url, body, headers = sent[0]
        assert url.endswith("secret-id") and json.loads(body)["type"] == "sync.finished"
        assert du_api.verify(body, secret, headers["X-Signature"])

        def fail(*_):
            raise RuntimeError("HTTP 500")
        events.publish(conn, events.SYNC_FINISHED, "Synced", {"id": "sync:4", "title": "Synced"})
        webhooks.deliver_due(conn, post=fail)
        row = conn.execute("SELECT attempts, done_at, next_at > ? FROM webhook_deliveries ORDER BY id DESC",
                           (events.now_iso(),)).fetchone()
        assert tuple(row) == (1, None, 1)                                            # retried in a minute
    assert "failed" in authed.get("/settings").get_data(as_text=True)


# --- images and the display page ---

def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (800, 450), (120, 60, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_episode_image_falls_back_to_show_art(app, lib, monkeypatch):
    from podbridge import artwork

    def fake_ensure(conn, art_dir, key, urls=None, session=None):
        if key != "podcast:pc-podcast-bb":
            return None
        art_dir.mkdir(parents=True, exist_ok=True)
        path = art_dir / "show.png"
        path.write_bytes(_png())
        return path
    monkeypatch.setattr(artwork, "ensure", fake_ensure)
    episode_id = lib.get("/api/v1/feeds/continue").get_json()["items"][0]["meta"]["episode_id"]
    r = lib.get(f"/api/v1/images/episode/{episode_id}?w=300", headers={"Accept": "image/webp"})
    assert r.status_code == 200 and r.mimetype == "image/webp"
    from PIL import Image
    assert Image.open(io.BytesIO(r.data)).width == 320                            # snapped
    assert lib.get("/api/v1/images/episode/99999").status_code == 404
    assert lib.get("/api/v1/images/poster/1").status_code == 404


def test_display_page(lib, anon):
    read = make_token(lib)
    r = anon.get("/display/continue-watching")
    assert r.status_code == 401 and r.mimetype == "text/html" and "read token" in r.get_data(as_text=True)
    r = anon.get(f"/display/continue-watching?token={read}&size=800x480&theme=light")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Referrer-Policy"] == "no-referrer"
    assert "Continue watching" in html and PEEP in html and 'data-theme="light"' in html
    assert "/api/v1/images/episode/" in html and f"token={read}" in html
    fragment = anon.get(f"/display/continue-watching?token={read}&fragment=1").get_data(as_text=True)
    assert "<html" not in fragment and PEEP in fragment


# --- schema ---

def test_migration_10_upgrades_an_old_database(tmp_path):
    conn = connect(tmp_path / "old.db")
    for version, path in migrations():
        if version > 9:
            break
        conn.executescript(f"BEGIN;{path.read_text(encoding='utf-8')}PRAGMA user_version = {version};COMMIT;")
    conn.execute("INSERT INTO settings (key, value) VALUES ('dry_run', '0')")
    conn.commit()
    assert migrate(conn) >= 10
    assert conn.execute("SELECT value FROM settings WHERE key = 'dry_run'").fetchone()[0] == "0"
    for table in ("api_tokens", "api_audit", "api_idempotency", "events", "webhooks", "webhook_deliveries"):
        conn.execute(f"SELECT * FROM {table}")
