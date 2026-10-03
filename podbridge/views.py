"""Dashboard, Settings and /healthz."""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from .auth import safe_next
from .crypto import SecretError
from .db import get_db, utcnow
from .discovery import check_patreon_session, discover_all
from .http import TransportError
from .patreon import PatreonBlocked, PatreonError, PatreonSessionExpired
from .services import NotConfigured, patreon_client
from .settings_store import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, SettingsStore, get_store

bp = Blueprint("main", __name__)

# Write-only secret fields on the Settings page, and the service whose
# verification status resets when they change.
SECRET_FIELDS = {
    "patreon_session_id": "patreon",
    "pocketcasts_email": "pocketcasts",
    "pocketcasts_password": "pocketcasts",
    "alert_webhook_url": None,
}

SERVICE_SECRETS = {
    "patreon": ("patreon_session_id",),
    "pocketcasts": ("pocketcasts_email", "pocketcasts_password"),
}


def connection_status(store: SettingsStore, service: str) -> dict:
    """'not_set' | 'unknown' | 'ok' | 'expired' | 'error', plus last verified time."""
    if not all(store.is_set(k) for k in SERVICE_SECRETS[service]):
        return {"state": "not_set", "verified_at": None}
    return {
        "state": store.get(f"{service}_status") or "unknown",
        "verified_at": store.get(f"{service}_verified_at"),
    }


def normalise_patreon_cookie(value: str) -> str:
    """Accept either the bare value or a pasted 'session_id=...' fragment."""
    value = value.strip().strip(";").strip()
    if value.lower().startswith("session_id="):
        value = value.split("=", 1)[1]
    return value.split(";", 1)[0].strip()


@bp.get("/")
def dashboard():
    store = get_store()
    db = get_db()
    last_run = db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM sources WHERE enabled = 1) AS sources, "
        "(SELECT COUNT(*) FROM episodes) AS episodes, "
        "(SELECT COUNT(*) FROM episodes e JOIN sources s ON s.id = e.source_id "
        " WHERE e.match_method = 'none' AND s.pocketcasts_podcast_uuid IS NOT NULL) AS unmatched"
    ).fetchone()
    return render_template(
        "dashboard.html",
        patreon=connection_status(store, "patreon"),
        pocketcasts=connection_status(store, "pocketcasts"),
        dry_run=store.get_bool("dry_run"),
        interval=store.get_int("sync_interval_minutes"),
        last_run=last_run,
        counts=counts,
    )


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    store = get_store()
    if request.method == "POST":
        errors = save_settings(store, request.form)
        for error in errors:
            flash(error, "error")
        if not errors:
            flash("Settings saved.", "ok")
        if request.form.get("action") == "test_patreon":
            run_patreon_test(store)
        return redirect(url_for("main.settings"))

    webhook_host = None
    if store.is_set("alert_webhook_url"):
        webhook_host = urlparse(store.get_secret("alert_webhook_url") or "").hostname
    secrets_state = {key: store.updated_at(key) for key in SECRET_FIELDS}
    return render_template(
        "settings.html",
        secrets_state=secrets_state,
        webhook_host=webhook_host,
        patreon=connection_status(store, "patreon"),
        pocketcasts=connection_status(store, "pocketcasts"),
        interval=store.get_int("sync_interval_minutes"),
        dry_run=store.get_bool("dry_run"),
        min_interval=MIN_INTERVAL_MINUTES,
        max_interval=MAX_INTERVAL_MINUTES,
    )


def save_settings(store: SettingsStore, form) -> list[str]:
    errors: list[str] = []
    changed: set[str] = set()

    for key, service in SECRET_FIELDS.items():
        if form.get(f"clear_{key}"):
            store.delete(key)
            if service:
                changed.add(service)
            continue
        raw = form.get(key, "")
        value = raw if key == "pocketcasts_password" else raw.strip()
        if not value:
            continue  # blank means "leave unchanged"
        if key == "patreon_session_id":
            value = normalise_patreon_cookie(value)
        if key == "alert_webhook_url" and urlparse(value).scheme not in ("http", "https"):
            errors.append("Alert webhook must be an http(s) URL.")
            continue
        store.set_secret(key, value)
        if service:
            changed.add(service)

    for service in changed:
        store.set(f"{service}_status", "unknown")
        store.delete(f"{service}_verified_at")
        if service == "pocketcasts":
            store.delete("pocketcasts_refresh_token")

    try:
        interval = int(form.get("sync_interval_minutes", ""))
        if not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            raise ValueError
        store.set("sync_interval_minutes", str(interval))
    except ValueError:
        errors.append(
            f"Sync interval must be a whole number of minutes "
            f"between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES}."
        )

    store.set("dry_run", "1" if form.get("dry_run") else "0")
    return errors


def patreon_failure(store: SettingsStore, exc: Exception) -> str:
    """Record what a Patreon failure means for connection status; return a user-facing message."""
    if isinstance(exc, NotConfigured):
        return str(exc)
    if isinstance(exc, SecretError):
        return "Stored Patreon cookie can't be decrypted (was ENCRYPTION_KEY changed?). Re-enter it in Settings."
    if isinstance(exc, PatreonSessionExpired):
        store.set("patreon_status", "expired")
        store.set("patreon_verified_at", utcnow())
        return "Patreon session has expired. Paste a fresh session_id cookie in Settings."
    if isinstance(exc, PatreonBlocked):
        return f"{exc}: Patreon is pushing back, so try again later."
    store.set("patreon_status", "error")
    return f"Couldn't reach Patreon: {exc}"


PATREON_FAILURES = (NotConfigured, SecretError, PatreonError, TransportError)


def run_patreon_test(store: SettingsStore) -> None:
    try:
        ok = check_patreon_session(store, patreon_client(store))
    except PATREON_FAILURES as exc:
        flash(patreon_failure(store, exc), "error")
        return
    if ok:
        flash("Patreon session is valid.", "ok")
    else:
        flash("Patreon session is not logged in. Paste a fresh session_id cookie.", "error")


EPISODE_FILTERS = {
    "all": "1 = 1",
    "in_progress": "p.patreon_watch_state = 'is_watching' AND COALESCE(p.patreon_is_watched, 0) = 0",
    "watched": "p.patreon_is_watched = 1",
    "unmatched": "e.match_method = 'none'",
}


@bp.get("/episodes")
def episodes():
    current = request.args.get("filter", "all")
    where = EPISODE_FILTERS.get(current, EPISODE_FILTERS["all"])
    db = get_db()
    sources = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
    rows = db.execute(
        "SELECT e.*, p.patreon_position_secs, p.patreon_is_watched, p.patreon_watch_state, "
        "p.patreon_updated_at, p.pocketcasts_position_secs, p.pocketcasts_status, p.last_synced_at "
        f"FROM episodes e LEFT JOIN progress p ON p.episode_id = e.id WHERE {where} "
        "ORDER BY e.published_at DESC"
    ).fetchall()
    by_source: dict[int, list] = {s["id"]: [] for s in sources}
    for row in rows:
        by_source.setdefault(row["source_id"], []).append(row)
    return render_template("episodes.html", sources=sources, by_source=by_source,
                           filters=list(EPISODE_FILTERS), current=current)


@bp.post("/episodes/refresh")
def refresh_episodes():
    store = get_store()
    try:
        results = discover_all(get_db(), store, patreon_client(store))
    except PATREON_FAILURES as exc:
        flash(patreon_failure(store, exc), "error")
    else:
        for r in results:
            flash(f"{r.label}: {r.posts_seen} posts, {r.added} new, {r.refreshed} refreshed"
                  + (f", {r.skipped_no_media} without media skipped" if r.skipped_no_media else ""), "ok")
        if not results:
            flash("No enabled sources to refresh.", "warn")
    nxt = request.form.get("next")
    return redirect(safe_next(nxt) if nxt else url_for("main.episodes"))


@bp.get("/sources")
def sources():
    db = get_db()
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM episodes e WHERE e.source_id = s.id) AS episode_count "
        "FROM sources s ORDER BY s.id"
    ).fetchall()
    campaign_id = request.args.get("campaign_id", "").strip()
    collections = None
    if campaign_id:
        if not campaign_id.isdigit():
            flash("Campaign ID must be a number.", "error")
        else:
            store = get_store()
            try:
                collections = patreon_client(store).list_collections(campaign_id)
            except PATREON_FAILURES as exc:
                flash(patreon_failure(store, exc), "error")
    return render_template("sources.html", sources=rows, campaign_id=campaign_id, collections=collections)


@bp.post("/sources")
def add_source():
    campaign_id = request.form.get("campaign_id", "").strip()
    collection_id = request.form.get("collection_id", "").strip()
    label = request.form.get("label", "").strip() or f"Campaign {campaign_id} / collection {collection_id}"
    if not (campaign_id.isdigit() and collection_id.isdigit()):
        flash("Campaign and collection IDs must be numbers.", "error")
        return redirect(url_for("main.sources", campaign_id=campaign_id))
    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO sources (label, campaign_id, collection_id) VALUES (?, ?, ?) "
            "ON CONFLICT (campaign_id, collection_id) DO NOTHING",
            (label, campaign_id, collection_id),
        )
    flash(f"Added {label}." if cur.rowcount else "That collection is already a source.",
          "ok" if cur.rowcount else "warn")
    return redirect(url_for("main.sources"))


@bp.post("/sources/<int:source_id>/toggle")
def toggle_source(source_id: int):
    db = get_db()
    with db:
        db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,))
    return redirect(url_for("main.sources"))


@bp.get("/healthz")
def healthz():
    """No auth. App up + whether each session was valid at last check; no detail."""
    get_db().execute("SELECT 1").fetchone()
    store = get_store()

    def valid(service: str) -> bool | None:
        state = connection_status(store, service)["state"]
        return {"ok": True, "expired": False, "error": False}.get(state)

    return jsonify(status="ok", patreon_session_valid=valid("patreon"),
                   pocketcasts_session_valid=valid("pocketcasts"))
