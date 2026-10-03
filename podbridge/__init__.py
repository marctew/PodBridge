"""PodBridge: sync Patreon playback progress into Pocket Casts."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, db, views
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

    zone = ZoneInfo(config.tz)

    @app.template_filter("localtime")
    def localtime(value: str | None) -> str:
        if not value:
            return "never"
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(zone).strftime("%d %b %Y %H:%M")

    db.init_app(app)
    auth.init_app(app)
    app.register_blueprint(views.bp)
    return app
