"""PodBridge: sync Patreon playback progress into Pocket Casts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask
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
    )
    # Behind a single reverse proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.extensions["secret_box"] = SecretBox(config.encryption_key)

    # Cache-busting: the stylesheet URL carries a hash of its contents, so browsers (Safari
    # especially) fetch the new file after every update instead of reusing a stale copy.
    css = Path(app.static_folder) / "style.css"
    app.jinja_env.globals["asset_version"] = (
        hashlib.sha1(css.read_bytes()).hexdigest()[:10] if css.is_file() else "dev")

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
    scheduler.init_scheduler(app)
    return app
