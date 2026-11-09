"""The API's routes: /api/v1/* (the standard's), /display/* (screens), and the forms behind the
API tokens and Webhooks cards in Settings. Thin: answers are built in feeds.py and actions.py.
`install(app)` wires du_api's gate in with PodBridge's tokens, session and CSRF."""

from __future__ import annotations

import hmac
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Blueprint, Flask, Response, current_app, flash, jsonify, redirect, render_template, request, session,
    url_for,
)

from .. import __version__, du_api, library
from ..db import connect, get_db
from ..du_api import display, standard
from . import actions, events, feeds, tokens, webhooks

api = Blueprint("api", __name__, url_prefix="/api/v1")
screens = Blueprint("display", __name__, url_prefix="/display")
cards = Blueprint("api_cards", __name__, url_prefix="/settings")

API_PREFIXES = ("/api/v1/", "/display/")
ICON = "/static/icon-192.png"


def _base() -> str:
    """PodBridge's address as this request reached it (remembered for events from the scheduler)."""
    base = request.host_url.rstrip("/")
    events.remember_base(get_db(), base)
    return base


def _busy() -> bool:
    from ..sync import _lock
    return _lock.locked()


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parents[2],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# -- the standard's routes -----------------------------------------------------------------------

@api.route("/meta")
@standard("read")
def meta():
    return jsonify(du_api.core().meta())


@api.route("/status")
@standard("read")
def status():
    from ..settings_store import get_store
    body = feeds.status(get_db(), get_store(), _base(), _busy())
    return du_api.cached(jsonify(body), body["refresh_s"])


@api.route("/feeds/<name>")
@standard("read")
def feed(name: str):
    if name not in feeds.FEEDS:
        return du_api.ApiCore.error("not_found", f"There's no feed called '{name}'. Feeds: "
                                    + ", ".join(feeds.FEEDS) + ".", 404)
    try:
        limit = int(request.args.get("limit") or feeds.DEFAULT_LIMIT)
    except ValueError:
        return du_api.ApiCore.error("bad_request", "limit must be a whole number.", 400)
    since = cursor = None
    if feeds.FEEDS[name].timed:
        for arg in ("since", "cursor"):
            raw = request.args.get(arg)
            if raw and du_api.parse_iso(raw) is None:
                return du_api.ApiCore.error("bad_request", f"{arg} must be a time like 2026-11-05T19:00:00Z.", 400)
        since = du_api.iso(du_api.parse_iso(request.args["since"])) if request.args.get("since") else None
        cursor = du_api.iso(du_api.parse_iso(request.args["cursor"])) if request.args.get("cursor") else None
    body = feeds.feed(get_db(), name, _base(), limit, since, cursor)
    return du_api.cached(jsonify(body), body["refresh_s"])


def _art_fetch(key: str | None):
    """The original artwork from PodBridge's own cache (fetched on first use, like /art/<key>)."""
    from .. import artwork
    from ..services import art_dir

    def fetch(_box: int) -> bytes | None:
        if not key or not artwork.KEY_PATTERN.match(key):
            return None
        conn = get_db()
        path = artwork.ensure(conn, art_dir(), key) if not key.startswith("patreon:") else None
        if path is None:
            row = artwork.cached(conn, key)
            path = art_dir() / row["filename"] if row is not None and row["ok"] and row["filename"] else None
        return path.read_bytes() if path is not None and path.is_file() else None
    return fetch


@api.route("/images/<kind>/<ident>")
@standard("read")
def image(kind: str, ident: str):
    cache = Path(current_app.config["PODBRIDGE"].database_path).parent / "image-cache"
    if kind == "episode" and ident.isdigit():
        row = get_db().execute(library.EPISODE_QUERY + " WHERE e.id = ?", (int(ident),)).fetchone()
        if row is None:
            return du_api.ApiCore.error("not_found", f"There's no episode {ident}.", 404)
        youtube = row["source_kind"] == "youtube"
        own = f"{'yt' if youtube else 'patreon'}:{row['patreon_post_id']}"
        show = f"podcast:{row['matched_podcast_uuid'] or row['pocketcasts_podcast_uuid']}" \
            if (row["matched_podcast_uuid"] or row["pocketcasts_podcast_uuid"]) else None
        own_fetch, show_fetch = _art_fetch(own), _art_fetch(show)

        def fetch(box: int) -> bytes | None:
            return own_fetch(box) or show_fetch(box)
        return du_api.image_response(cache, kind, f"{ident}:{own}:{show}", fetch)
    if kind == "show" and re.fullmatch(r"[A-Za-z0-9-]{1,64}", ident):
        return du_api.image_response(cache, kind, ident, _art_fetch(f"podcast:{ident}"))
    return du_api.ApiCore.error("not_found", "Image kinds: episode/<episode id>, show/<podcast uuid>.", 404)


@api.route("/actions/<name>", methods=["POST"])
@standard("write")
def action(name: str):
    who = du_api.identity()
    body = request.get_json(silent=True) if request.get_data() else {}
    status_code, out, retry_after = actions.run(
        get_db(), name, body, token_id=who.token_id, token_name=who.name or "A token",
        key=request.headers.get("Idempotency-Key"), over_limit=du_api.core().over_limit)
    resp = jsonify(out)
    resp.status_code = status_code
    if retry_after is not None or status_code in (429, 503):
        resp.headers["Retry-After"] = str(retry_after or 30)
    return resp


@api.route("/events")
@standard("read")
def events_stream():
    core = du_api.core()
    release = core.streams.take()
    if release is None:
        return du_api.ApiCore.error("busy", "Too many open event streams. Try again shortly.", 503, retry_after=30)
    raw = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    try:
        last = int(raw) if raw not in (None, "") else events.last_id(get_db())
    except ValueError:
        release()
        return du_api.ApiCore.error("bad_request", "Last-Event-ID must be a number.", 400)
    path = current_app.config["PODBRIDGE"].database_path

    def generate():
        conn = connect(path)            # its own: the request's connection closes before the stream ends
        try:
            yield from du_api.event_stream(lambda after: events.after(conn, after), last, stopping=core.stopping)
        finally:
            conn.close()
    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.call_on_close(release)
    return resp


# -- display pages -----------------------------------------------------------------------------------

ACCENTS = ("violet", "blue", "graphite", "teal", "fuchsia")      # DESIGN-SPEC 3.3; violet is Media, PodBridge's own
SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")


def _options() -> dict:
    a = request.args
    size = "m"
    m = SIZE_RE.match(a.get("size") or "")
    if m:
        width = int(m.group(1))
        size = "s" if width <= 480 else "m" if width <= 1100 else "l"
    try:
        scale = min(3.0, max(0.5, float(a.get("scale") or 1)))
    except ValueError:
        scale = 1.0
    return {"theme": a.get("theme") if a.get("theme") in ("dark", "light") else "dark", "size": size,
            "scale": scale, "accent": a.get("accent") if a.get("accent") in ACCENTS else "violet",
            "static": a.get("static") in ("1", "true")}


def _with_token(path: str, **params) -> str:
    q = {k: v for k, v in params.items() if v is not None}
    if request.args.get("token"):
        q["token"] = request.args["token"]
    return path + ("?" + urlencode(q) if q else "")


@screens.route("/continue-watching")
@display
def continue_watching():
    opts = _options()
    body = feeds.feed(get_db(), "continue", _base(), limit={"s": 3, "m": 6, "l": 8}[opts["size"]])
    width = {"s": 320, "m": 640, "l": 640}[opts["size"]]
    page = {"items": body["items"], "refresh_s": body["refresh_s"],
            "img": lambda path: _with_token(path, w=width) if path else None}
    keep = {k: request.args[k] for k in ("theme", "size", "scale", "accent") if k in request.args}
    template = ("display/continue_watching_body.html" if request.args.get("fragment") == "1"
                else "display/continue_watching.html")
    return render_template(template, opts=opts, page=page, title="Continue watching",
                           stylesheets=[url_for("static", filename="design/uniform.css"),  # ?v=<mtime> from du_flask
                                        url_for("static", filename="display.css")],
                           fragment_url=_with_token(request.path, fragment=1, **keep),
                           events_url=_with_token("/api/v1/events"),
                           event_names=[events.PROGRESS_SYNCED, events.SYNC_FINISHED])


# -- Settings: the API tokens and Webhooks cards' forms -------------------------------------------------

def card_context(token_new: dict | None = None, webhook_new: dict | None = None) -> dict:
    """What partials/api_tokens_card.html and webhooks_card.html need (they document it at the top)."""
    conn = get_db()
    ago = webhooks._ago
    return {
        "api_tokens": [{**t, "last_used": ago(t["last_used_at"]) or None} for t in tokens.list_tokens(conn)],
        "api_token_new": token_new,
        "api_urls": {"create": url_for("api_cards.token_create"),
                     "revoke": url_for("api_cards.token_revoke", token_id=0).replace("/0/", "/{id}/")},
        "api_displays": [{"title": "Continue watching", "url": url_for("display.continue_watching")}],
        "api_actions": [{"action": a["action"], "message": a["message"], "token_name": a["token_name"],
                         "when": ago(a["at"]), "ok": a["status"] < 400} for a in actions.recent(conn)],
        "webhooks": webhooks.list_webhooks(conn),
        "webhook_new": webhook_new,
        "webhook_event_types": events.TYPES,
        "webhook_urls": {"create": url_for("api_cards.webhook_create"),
                         "secret": url_for("api_cards.webhook_secret", webhook_id=0).replace("/0/", "/{id}/"),
                         "delete": url_for("api_cards.webhook_delete", webhook_id=0).replace("/0/", "/{id}/")},
    }


def _settings_page(**new):
    from ..views import settings_page_context
    return render_template("settings.html", **settings_page_context(), **card_context(**new))


@cards.post("/api/tokens")
def token_create():
    try:
        token = tokens.create(get_db(), request.form.get("name", ""), request.form.get("scope", "read"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.settings", _anchor="api"))
    return _settings_page(token_new={"token": token, "name": request.form.get("name", "").strip()})


@cards.post("/api/tokens/<int:token_id>/revoke")
def token_revoke(token_id: int):
    flash("Token revoked." if tokens.revoke(get_db(), token_id) else "That token was already gone.", "ok")
    return redirect(url_for("main.settings", _anchor="api"))


@cards.post("/webhooks")
def webhook_create():
    try:
        secret = webhooks.create(get_db(), request.form.get("name", ""), request.form.get("url", ""),
                                 request.form.getlist("event_types"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.settings", _anchor="webhooks"))
    return _settings_page(webhook_new={"secret": secret, "name": request.form.get("name", "").strip()})


@cards.post("/webhooks/<int:webhook_id>/secret")
def webhook_secret(webhook_id: int):
    conn = get_db()
    name, secret = webhooks.name_of(conn, webhook_id), webhooks.new_secret(conn, webhook_id)
    if secret is None:
        flash("That webhook was already gone.", "warn")
        return redirect(url_for("main.settings", _anchor="webhooks"))
    return _settings_page(webhook_new={"secret": secret, "name": name})


@cards.post("/webhooks/<int:webhook_id>/delete")
def webhook_delete(webhook_id: int):
    flash("Webhook deleted." if webhooks.delete(get_db(), webhook_id) else "That webhook was already gone.", "ok")
    return redirect(url_for("main.settings", _anchor="webhooks"))


# -- wiring --------------------------------------------------------------------------------------

def is_api_request() -> bool:
    return request.path.startswith(API_PREFIXES)


def housekeeping(app: Flask) -> None:
    """Every minute from the scheduler: send due webhooks, prune old events, keys, log and images."""
    with app.app_context():
        conn = get_db()
        webhooks.deliver_due(conn)
        events.prune(conn)
        webhooks.prune(conn)
        actions.prune(conn)


def prune_images(app: Flask) -> None:
    with app.app_context():
        du_api.prune_image_cache(Path(app.config["PODBRIDGE"].database_path).parent / "image-cache")


def install(app: Flask) -> None:
    def csrf_ok() -> bool:
        sent = request.headers.get("X-CSRF-Token", "")
        return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))

    sha = _git_sha()
    core = du_api.init_api(
        app, name="podbridge", title="PodBridge", version=__version__, sha=lambda: sha,
        accent="violet", icon=ICON, time_zone=lambda: app.config["PODBRIDGE"].tz,
        check_token=lambda t: tokens.check(get_db(), t),
        is_admin_session=lambda: bool(session.get("auth")),
        csrf_ok=csrf_ok,
    )
    core.unmetered_endpoints.update({"api.image", "api.events_stream"})
    for f in feeds.FEEDS.values():
        params = {"limit": "int (default 20, at most 100)"}
        if f.timed:
            params.update(since="ISO time", cursor="from next_cursor")
        core.add_feed(f.name, f.title, f.refresh_s, params)
    for a in actions.ACTIONS.values():
        core.add_action(a.name, a.title, a.body, limit=a.limit)
    core.add_display("continue-watching", "Continue watching", ["320x240", "800x480", "1920x1080"])
    core.add_events(events.TYPES)
    app.register_blueprint(api)
    app.register_blueprint(screens)
    app.register_blueprint(cards)
    if app.config["PODBRIDGE"].scheduler_enabled:   # off in tests and the dev server
        du_api.stop_streams_on_sigterm(core)
