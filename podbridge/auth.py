"""Single-password login, CSRF tokens and a simple failed-login lockout.

Every endpoint requires login except those in PUBLIC_ENDPOINTS. Every POST
requires a valid CSRF token, including the login form.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import deque

from flask import (
    Blueprint, Flask, abort, current_app, redirect, render_template, request, session, url_for,
)

bp = Blueprint("auth", __name__)

PUBLIC_ENDPOINTS = frozenset({"auth.login", "main.healthz", "static"})
MAX_FAILURES = 10
FAILURE_WINDOW_SECS = 15 * 60


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def _check_csrf() -> None:
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.dashboard")


def _failures() -> dict[str, deque[float]]:
    return current_app.extensions.setdefault("login_failures", {})


def _recent_failures(ip: str) -> deque[float]:
    attempts = _failures().setdefault(ip, deque())
    cutoff = time.monotonic() - FAILURE_WINDOW_SECS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


def _require_login():
    if request.method == "POST":
        _check_csrf()
    if request.endpoint in PUBLIC_ENDPOINTS or session.get("auth"):
        return None
    nxt = request.full_path.rstrip("?") if request.method == "GET" else None
    return redirect(url_for("auth.login", next=nxt))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("auth"):
        return redirect(url_for("main.dashboard"))
    if request.method == "GET":
        return render_template("login.html")

    ip = request.remote_addr or "unknown"
    attempts = _recent_failures(ip)
    if len(attempts) >= MAX_FAILURES:
        return render_template("login.html", error="Too many attempts. Try again in 15 minutes."), 429

    supplied = request.form.get("password", "").encode()
    expected = current_app.config["PODBRIDGE"].app_password.encode()
    if hmac.compare_digest(supplied, expected):
        _failures().pop(ip, None)
        session.clear()
        session.permanent = True
        session["auth"] = True
        csrf_token()
        return redirect(_safe_next(request.args.get("next")))

    attempts.append(time.monotonic())
    time.sleep(current_app.config["PODBRIDGE"].login_failure_delay)
    return render_template("login.html", error="Wrong password."), 401


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def init_app(app: Flask) -> None:
    app.before_request(_require_login)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.register_blueprint(bp)
