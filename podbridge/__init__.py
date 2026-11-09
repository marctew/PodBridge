"""PodBridge: sync Patreon playback progress into Pocket Casts."""

from __future__ import annotations

__version__ = "0.1.0"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, db, scheduler, views
from .config import Config
from .crypto import SecretBox
from .redact import configure_logging


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    configure_logging()

    app = Flask(__name__)
    app.config.update(
        PODBRIDGE=config,
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,  # backup restores
    )
    # Behind a single reverse proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.extensions["secret_box"] = SecretBox(config.encryption_key)

    # Caching (APP-STANDARD 12): pages are no-store, static files no-cache (the browser keeps them and
    # revalidates, so a deploy's CSS and JS show at once), and du_flask's init_design puts ?v=<mtime> on
    # every url_for('static', ...) link. A response that sets its own caching (the events stream, API
    # images and feeds, display pages from du_api) keeps its max-age but is made private.
    @app.after_request
    def cache_headers(resp):
        # APP-STANDARD 12: static files revalidate; everything else is someone's own and never goes in a shared
        # cache. Unmarked becomes no-store (pages, JSON, redirects, errors); what sets its own caching keeps its
        # max-age but is made private. Through the tunnel, Cloudflare stores an unmarked answer and hands it to
        # anyone, signed in or not (Encore, 9 Nov 2026).
        if request.endpoint == "static":
            resp.headers["Cache-Control"] = "no-cache"   # revalidate, so a deploy's CSS and JS show at once
            return resp
        words = [w.strip() for w in resp.headers.get("Cache-Control", "").split(",")
                 if w.strip() and w.strip().lower() != "public"]
        if not words:
            words = ["no-store"]
        elif not {"private", "no-store"} & {w.lower() for w in words}:
            words.insert(0, "private")
        resp.headers["Cache-Control"] = ", ".join(words)
        return resp

    zone = ZoneInfo(config.tz)

    @app.template_filter("localtime")
    def localtime(value: str | None, date_only: bool = False) -> str:
        if not value:
            return "never"
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return moment.astimezone(zone).strftime("%d %b %Y" if date_only else "%d %b %Y %H:%M")

    @app.template_filter("hm")
    def hm(secs: float | None) -> str:
        """Durations for stats: '3h 20m', '45m', '0m'."""
        minutes = int(round((secs or 0) / 60))
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02}m" if hours else f"{minutes}m"

    @app.template_filter("hms")
    def hms(value: float | None) -> str:
        if value is None:
            return "–"
        total = int(value)
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    db.init_app(app)
    auth.init_app(app)
    app.register_blueprint(views.bp)
    from .api import web as api_web  # the outside API (docs/API.md), on Design Uniformity's du_api
    api_web.install(app)
    try:  # the Design system card in Settings; du_flask.py is copied in by the design updater
        from .du_flask import init_design
        init_design(app, config.database_path.parent / "design", version=__version__)
    except ImportError:
        pass
    # v= on the home screen icon and manifest (PWA-ICONS.md 2); du_flask sets it, this covers its absence.
    app.jinja_env.globals["app_version"] = app.jinja_env.globals.get("app_version") or __version__
    scheduler.init_scheduler(app)
    return app
