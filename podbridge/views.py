"""Web UI routes and /healthz."""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from .auth import safe_next
from .crypto import SecretError
from .db import get_db, utcnow
from .discovery import check_patreon_session, discover_all
from .http import TransportError
from .linking import (
    MatchError, allow_auto_match, apply_pocketcasts_state, check_pocketcasts_login, link_source, refresh_all,
    set_manual_match, unlink, widen_source,
)
from .patreon import PatreonBlocked, PatreonError, PatreonSessionExpired
from .pocketcasts import PocketCastsAuthError, PocketCastsBlocked, PocketCastsError
from .matching import title_similarity
from .resume import TIMESTAMP_PARAM_PATTERN, build_resume_url, last_touched, resume_position
from .scheduler import next_run_at, restart_countdown, run_sync_now, scheduler_running
from .sync import SyncBusy
from .services import (
    NotConfigured, patreon_client, pocketcasts_client, pocketcasts_configured, pocketcasts_tokens, youtube_client,
)
from .discovery import check_youtube_session, discover_youtube_all, has_sources
from .youtube import YouTubeBlocked, YouTubeError, YouTubeSessionExpired
from .settings_store import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, SettingsStore, get_store

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

# Write-only secret fields on the Settings page, and the service whose
# verification status resets when they change.
SECRET_FIELDS = {
    "patreon_session_id": "patreon",
    "pocketcasts_email": "pocketcasts",
    "pocketcasts_password": "pocketcasts",
    "youtube_cookies": "youtube",
    "alert_webhook_url": None,
}

SERVICE_SECRETS = {
    "patreon": ("patreon_session_id",),
    "pocketcasts": ("pocketcasts_email", "pocketcasts_password"),
    "youtube": ("youtube_cookies",),
}

PATREON_FAILURES = (NotConfigured, SecretError, PatreonError, TransportError)
POCKETCASTS_FAILURES = (NotConfigured, SecretError, PocketCastsError, TransportError)
YOUTUBE_FAILURES = (NotConfigured, SecretError, YouTubeError, TransportError)


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


# --- failure reporting ---

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


def pocketcasts_failure(store: SettingsStore, exc: Exception) -> str:
    if isinstance(exc, NotConfigured):
        return str(exc)
    if isinstance(exc, SecretError):
        return "Stored Pocket Casts credentials can't be decrypted (was ENCRYPTION_KEY changed?). Re-enter them."
    if isinstance(exc, PocketCastsAuthError):
        store.set("pocketcasts_status", "error")
        store.set("pocketcasts_verified_at", utcnow())
        return "Pocket Casts rejected the email or password. Check them in Settings."
    if isinstance(exc, PocketCastsBlocked):
        return f"{exc}: Pocket Casts is pushing back, so try again later."
    store.set("pocketcasts_status", "error")
    return f"Pocket Casts problem: {exc}"


def youtube_failure(store: SettingsStore, exc: Exception) -> str:
    if isinstance(exc, NotConfigured):
        return str(exc)
    if isinstance(exc, SecretError):
        return "Stored YouTube cookies can't be decrypted (was ENCRYPTION_KEY changed?). Paste them again."
    if isinstance(exc, YouTubeSessionExpired):
        store.set("youtube_status", "expired")
        store.set("youtube_verified_at", utcnow())
        return f"YouTube login has expired: {exc}. Export fresh cookies and paste them in Settings."
    if isinstance(exc, YouTubeBlocked):
        return f"{exc}: YouTube is pushing back, so try again later."
    store.set("youtube_status", "error")
    return f"YouTube problem: {exc}"


# --- resume links ---

EPISODE_QUERY = (
    "SELECT e.*, s.pocketcasts_podcast_uuid, s.enabled AS source_enabled, s.kind AS source_kind, "
    "p.patreon_position_secs, p.patreon_is_watched, p.patreon_watch_state, p.patreon_updated_at, "
    "p.pocketcasts_position_secs, p.pocketcasts_status, p.pocketcasts_changed_at, p.last_synced_at "
    "FROM episodes e JOIN sources s ON s.id = e.source_id LEFT JOIN progress p ON p.episode_id = e.id"
)


def annotate(row, param: str) -> dict:
    """Row -> dict with resume position, Continue-on-Patreon URL and last-touched time."""
    ep = dict(row)
    youtube = ep.get("source_kind") == "youtube"
    ep["source_name"] = "YouTube" if youtube else "Patreon"
    ep["resume"] = resume_position(ep["patreon_position_secs"], bool(ep["patreon_is_watched"]),
                                   ep["duration_secs"], ep["pocketcasts_status"], ep["pocketcasts_position_secs"])
    ep["resume_url"] = (build_resume_url(ep["patreon_url"], ep["resume"].position_secs, "t" if youtube else param)
                        if ep["resume"] and ep["patreon_url"] else None)
    ep["touched"] = last_touched(ep["patreon_updated_at"], ep["pocketcasts_changed_at"])
    return ep


def continue_watching(limit: int | None = 5) -> list[dict]:
    param = get_store().get("patreon_timestamp_param")
    rows = get_db().execute(EPISODE_QUERY + " WHERE s.enabled = 1").fetchall()
    eps = [annotate(r, param) for r in rows]
    eps = [e for e in eps if e["resume"] and e["resume"].position_secs > 0 and e["resume_url"]]
    eps.sort(key=lambda e: e["touched"].timestamp() if e["touched"] else 0, reverse=True)
    return eps[:limit] if limit else eps


def refresh_pocketcasts_states(episode_id: int | None = None) -> None:
    """Fresh Pocket Casts state at click time (spec 9a). Failures fall back to the cached state."""
    store = get_store()
    if not pocketcasts_configured(store):
        return
    db = get_db()
    try:
        client = pocketcasts_client(store)
        if episode_id is not None:
            row = db.execute(EPISODE_QUERY + " WHERE e.id = ?", (episode_id,)).fetchone()
            if row is None or not (row["pocketcasts_episode_uuid"] and row["pocketcasts_podcast_uuid"]):
                return
            state = client.get_episode_state(row["pocketcasts_episode_uuid"], row["pocketcasts_podcast_uuid"])
            if state:
                with db:
                    apply_pocketcasts_state(db, episode_id, state.status, state.played_up_to)
            return
        sources = db.execute("SELECT id, pocketcasts_podcast_uuid FROM sources WHERE enabled = 1 "
                             "AND pocketcasts_podcast_uuid IS NOT NULL").fetchall()
        for source in sources:
            states = client.episode_states(source["pocketcasts_podcast_uuid"])
            matched = db.execute("SELECT id, pocketcasts_episode_uuid FROM episodes WHERE source_id = ? "
                                 "AND pocketcasts_episode_uuid IS NOT NULL", (source["id"],)).fetchall()
            with db:
                for ep in matched:
                    state = states.get(ep["pocketcasts_episode_uuid"])
                    apply_pocketcasts_state(db, ep["id"], state.status if state else 1,
                                            state.played_up_to if state else 0.0)
    except POCKETCASTS_FAILURES as exc:
        log.warning("Fresh Pocket Casts state unavailable, using cached: %s", type(exc).__name__)


@bp.get("/go/latest")
def go_latest():
    refresh_pocketcasts_states()
    eps = continue_watching(limit=1)
    if not eps:
        flash("Nothing in progress to continue.", "warn")
        return redirect(url_for("main.dashboard"))
    return redirect(eps[0]["resume_url"], code=302)


@bp.get("/go/<int:episode_id>")
def go_episode(episode_id: int):
    refresh_pocketcasts_states(episode_id)
    row = get_db().execute(EPISODE_QUERY + " WHERE e.id = ?", (episode_id,)).fetchone()
    if row is None:
        abort(404)
    ep = annotate(row, get_store().get("patreon_timestamp_param"))
    if not ep["patreon_url"]:
        flash("This episode has no Patreon link.", "error")
        return redirect(url_for("main.episodes"))
    return redirect(ep["resume_url"] or ep["patreon_url"], code=302)


# --- dashboard and settings ---

@bp.get("/")
def dashboard():
    store = get_store()
    db = get_db()
    last_run = db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM sources WHERE enabled = 1) AS sources, "
        "(SELECT COUNT(*) FROM episodes) AS episodes, "
        "(SELECT COUNT(*) FROM episodes WHERE match_method != 'none') AS matched, "
        "(SELECT COUNT(*) FROM episodes e JOIN sources s ON s.id = e.source_id "
        " WHERE e.match_method = 'none' AND e.match_locked = 0 "
        " AND s.pocketcasts_podcast_uuid IS NOT NULL) AS unmatched"
    ).fetchone()
    return render_template(
        "dashboard.html",
        patreon=connection_status(store, "patreon"),
        pocketcasts=connection_status(store, "pocketcasts"),
        youtube=connection_status(store, "youtube"),
        dry_run=store.get_bool("dry_run"),
        interval=store.get_int("sync_interval_minutes"),
        last_run=last_run,
        counts=counts,
        next_run=next_run_at(),
        scheduler_on=scheduler_running(),
        watching=continue_watching(),
    )


@bp.post("/sync")
def sync_now():
    """Manual sync. Afterwards the automatic countdown restarts from a full interval."""
    try:
        summary = run_sync_now("manual")
    except SyncBusy:
        flash("A sync is already running. Try again in a moment.", "warn")
    except NotConfigured as exc:
        flash(str(exc), "error")
    except SecretError:
        flash("Stored credentials can't be decrypted (was ENCRYPTION_KEY changed?). Re-enter them.", "error")
    else:
        restart_countdown()
        if summary.status == "ok":
            parts = [f"{n} {action.replace('_', ' ')}" for action, n in sorted(summary.actions.items())]
            flash(f"Sync {'(dry run) ' if summary.dry_run else ''}done: {summary.checked} checked, "
                  f"{summary.updated} written to Pocket Casts" + (f" · {', '.join(parts)}" if parts else ""), "ok")
        else:
            flash(f"Sync {summary.status}: {summary.error}", "error")
    nxt = request.form.get("next")
    return redirect(safe_next(nxt) if nxt else url_for("main.dashboard"))


@bp.get("/activity")
def activity():
    db = get_db()
    runs = db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 50").fetchall()
    events: dict[int, list] = {r["id"]: [] for r in runs}
    if runs:
        rows = db.execute(
            "SELECT ev.*, e.title FROM sync_events ev LEFT JOIN episodes e ON e.id = ev.episode_id "
            f"WHERE ev.run_id IN ({','.join('?' * len(runs))}) ORDER BY ev.id",
            [r["id"] for r in runs]).fetchall()
        for row in rows:
            events[row["run_id"]].append(row)
    return render_template("activity.html", runs=runs, events=events)


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    store = get_store()
    if request.method == "POST":
        old_interval = store.get_int("sync_interval_minutes")
        errors = save_settings(store, request.form)
        if store.get_int("sync_interval_minutes") != old_interval:
            restart_countdown()
        for error in errors:
            flash(error, "error")
        if not errors:
            flash("Settings saved.", "ok")
        action = request.form.get("action")
        if action == "test_patreon":
            run_patreon_test(store)
        elif action == "test_pocketcasts":
            run_pocketcasts_test(store)
        elif action == "test_youtube":
            run_youtube_test(store)
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
        youtube=connection_status(store, "youtube"),
        interval=store.get_int("sync_interval_minutes"),
        dry_run=store.get_bool("dry_run"),
        timestamp_param=store.get("patreon_timestamp_param"),
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
            pocketcasts_tokens().clear()

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

    param = form.get("patreon_timestamp_param", "").strip()
    if param:
        if TIMESTAMP_PARAM_PATTERN.match(param):
            store.set("patreon_timestamp_param", param)
        else:
            errors.append("Timestamp parameter must be a short name like t or start.")
    return errors


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


def run_pocketcasts_test(store: SettingsStore) -> None:
    try:
        check_pocketcasts_login(store, pocketcasts_client(store))
    except POCKETCASTS_FAILURES as exc:
        flash(pocketcasts_failure(store, exc), "error")
        return
    flash("Pocket Casts login works.", "ok")


def run_youtube_test(store: SettingsStore) -> None:
    try:
        ok = check_youtube_session(store, youtube_client(store))
    except YOUTUBE_FAILURES as exc:
        flash(youtube_failure(store, exc), "error")
        return
    if ok:
        flash("YouTube cookies are signed in.", "ok")
    else:
        flash("YouTube cookies aren't signed in. Export them again from a fresh private window.", "error")


# --- episodes ---

EPISODE_FILTERS = {
    "all": "1 = 1",
    "in_progress": "p.patreon_watch_state = 'is_watching' AND COALESCE(p.patreon_is_watched, 0) = 0",
    "watched": "p.patreon_is_watched = 1",
    "unmatched": "e.match_method = 'none' AND e.match_locked = 0",
}


@bp.get("/episodes")
def episodes():
    current = request.args.get("filter", "all")
    where = EPISODE_FILTERS.get(current, EPISODE_FILTERS["all"])
    db = get_db()
    sources = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
    rows = db.execute(f"{EPISODE_QUERY} WHERE {where} ORDER BY e.published_at DESC").fetchall()
    param = get_store().get("patreon_timestamp_param")
    by_source: dict[int, list] = {s["id"]: [] for s in sources}
    for row in rows:
        by_source.setdefault(row["source_id"], []).append(annotate(row, param))
    return render_template("episodes.html", sources=sources, by_source=by_source,
                           filters=list(EPISODE_FILTERS), current=current)


@bp.post("/episodes/refresh")
def refresh_episodes():
    """Patreon and YouTube discovery, then Pocket Casts catalogue + auto-matching."""
    store = get_store()
    db = get_db()
    if has_sources(db, "patreon"):
        try:
            for r in discover_all(db, store, patreon_client(store)):
                flash(f"Patreon · {r.label}: {r.posts_seen} posts, {r.added} new"
                      + (f", {r.skipped_no_media} without media skipped" if r.skipped_no_media else ""), "ok")
        except PATREON_FAILURES as exc:
            flash(patreon_failure(store, exc), "error")
    if has_sources(db, "youtube"):
        try:
            for r in discover_youtube_all(db, store, youtube_client(store)):
                flash(f"YouTube · {r.label}: {r.posts_seen} watched videos in recent history, {r.added} new", "ok")
        except YOUTUBE_FAILURES as exc:
            flash(youtube_failure(store, exc), "error")

    linked = db.execute("SELECT COUNT(*) FROM sources WHERE enabled = 1 "
                        "AND pocketcasts_podcast_uuid IS NOT NULL").fetchone()[0]
    if not pocketcasts_configured(store):
        flash("Pocket Casts isn't set up yet, so matching was skipped.", "warn")
    elif not linked:
        flash("No source is linked to a Pocket Casts podcast yet. Link one on the Sources page.", "warn")
    else:
        try:
            for r in refresh_all(db, store, pocketcasts_client(store)):
                flash(f"Pocket Casts · {r.label}: {r.catalogue_size} episodes in feed, "
                      f"{r.matched_total} matched ({r.newly_matched} new), {r.unmatched} unmatched", "ok")
                if r.catalogue_truncated:
                    flash("Pocket Casts says the feed has more episodes than it returned; "
                          "older episodes may not match.", "warn")
        except POCKETCASTS_FAILURES as exc:
            flash(pocketcasts_failure(store, exc), "error")

    nxt = request.form.get("next")
    return redirect(safe_next(nxt) if nxt else url_for("main.episodes"))


def _days_apart(a: str | None, b: str | None) -> float:
    try:
        da = datetime.fromisoformat((a or "").replace("Z", "+00:00"))
        db_ = datetime.fromisoformat((b or "").replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return abs((da - db_).total_seconds()) / 86400


@bp.get("/episodes/<int:episode_id>/match")
def match_episode(episode_id: int):
    db = get_db()
    episode = db.execute(
        "SELECT e.*, s.label AS source_label, s.pocketcasts_podcast_uuid FROM episodes e "
        "JOIN sources s ON s.id = e.source_id WHERE e.id = ?", (episode_id,)).fetchone()
    if episode is None:
        abort(404)
    candidates = []
    if episode["pocketcasts_podcast_uuid"]:
        rows = db.execute(
            "SELECT pe.*, other.id AS taken_by_id, other.title AS taken_by_title FROM pocketcasts_episodes pe "
            "LEFT JOIN (SELECT e.id, e.title, e.pocketcasts_episode_uuid FROM episodes e "
            "           JOIN sources s ON s.id = e.source_id WHERE s.enabled = 1) other "
            "  ON other.pocketcasts_episode_uuid = pe.uuid AND other.id != ? "
            "WHERE pe.podcast_uuid = ?", (episode_id, episode["pocketcasts_podcast_uuid"])).fetchall()
        candidates = sorted(rows, key=lambda r: (r["taken_by_id"] is not None,
                                                 _days_apart(r["published_at"], episode["published_at"])))
    return render_template("match.html", episode=episode, candidates=candidates)


@bp.post("/episodes/<int:episode_id>/match")
def save_match(episode_id: int):
    db = get_db()
    try:
        if request.form.get("action") == "unlink":
            unlink(db, episode_id)
            flash("Unlinked. Auto-matching will leave this episode alone.", "ok")
        elif request.form.get("action") == "allow_auto":
            allow_auto_match(db, episode_id)
            flash("Auto-matching re-enabled for this episode; it applies on the next refresh.", "ok")
        else:
            set_manual_match(db, episode_id, request.form.get("uuid", ""))
            flash("Matched.", "ok")
    except MatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.match_episode", episode_id=episode_id))
    return redirect(url_for("main.episodes"))


# --- sources ---

@bp.get("/sources")
def sources():
    db = get_db()
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM episodes e WHERE e.source_id = s.id) AS episode_count, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.source_id = s.id AND e.match_method != 'none') AS matched_count "
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
    if collection_id == "all":
        collection_id = ""
    default_label = (f"Campaign {campaign_id} / all posts" if not collection_id
                     else f"Campaign {campaign_id} / collection {collection_id}")
    label = (request.form.get("label", "").strip() or request.form.get("default_label", "").strip()
             or default_label)
    if not campaign_id.isdigit() or (collection_id and not collection_id.isdigit()):
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


@bp.post("/sources/youtube")
def add_youtube_source():
    handle = request.form.get("channel", "").strip()
    if not handle:
        flash("Enter a channel handle (like @TheNewsAgents) or channel URL.", "error")
        return redirect(url_for("main.sources"))
    store = get_store()
    try:
        channel = youtube_client(store).resolve_channel(handle)
    except YOUTUBE_FAILURES as exc:
        flash(youtube_failure(store, exc), "error")
        return redirect(url_for("main.sources"))
    if channel is None:
        flash(f"Couldn't find a YouTube channel for “{handle}”.", "error")
        return redirect(url_for("main.sources"))
    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO sources (label, campaign_id, collection_id, kind) VALUES (?, ?, '', 'youtube') "
            "ON CONFLICT (campaign_id, collection_id) DO NOTHING", (f"YouTube: {channel.title}", channel.channel_id))
    if cur.rowcount:
        flash(f"Added YouTube: {channel.title}. Now link it to its Pocket Casts podcast.", "ok")
    else:
        flash("That channel is already a source.", "warn")
    return redirect(url_for("main.sources"))


@bp.post("/sources/<int:source_id>/widen")
def widen_source_route(source_id: int):
    db = get_db()
    try:
        widen_source(db, source_id)
    except MatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.sources"))
    others = db.execute(
        "SELECT COUNT(*) FROM sources WHERE id != ? AND enabled = 1 AND campaign_id = "
        "(SELECT campaign_id FROM sources WHERE id = ?)", (source_id, source_id)).fetchone()[0]
    flash("Now covers every post in the campaign. Existing matches are kept; press “Refresh” to pull in the rest."
          + (" Disable the other sources for this campaign so posts aren't counted twice." if others else ""), "ok")
    return redirect(url_for("main.sources"))


@bp.get("/sources/<int:source_id>/link")
def link_source_form(source_id: int):
    source = get_db().execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        abort(404)
    store = get_store()
    try:
        podcasts = pocketcasts_client(store).list_podcasts()
    except POCKETCASTS_FAILURES as exc:
        flash(pocketcasts_failure(store, exc), "error")
        return redirect(url_for("main.sources"))
    if source["kind"] == "youtube":
        # Closest title to the channel name first.
        channel = source["label"].removeprefix("YouTube: ")
        podcasts.sort(key=lambda p: (-title_similarity(channel, p.title), p.title.casefold()))
    else:
        # Private Patreon feeds first: that's almost always the right target.
        podcasts.sort(key=lambda p: (p.feed_host != "www.patreon.com", p.title.casefold()))
    return render_template("link_source.html", source=source, podcasts=podcasts)


@bp.post("/sources/<int:source_id>/link")
def link_source_save(source_id: int):
    uuid = request.form.get("podcast_uuid", "").strip()
    if not uuid:
        flash("Pick a podcast.", "error")
        return redirect(url_for("main.link_source_form", source_id=source_id))
    try:
        link_source(get_db(), source_id, uuid)
    except MatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.sources"))
    flash("Linked. Use “Refresh” on the Episodes page to load the feed and match episodes.", "ok")
    return redirect(url_for("main.sources"))


# --- health ---

@bp.get("/healthz")
def healthz():
    """No auth. App up + whether each session was valid at last check; no detail."""
    get_db().execute("SELECT 1").fetchone()
    store = get_store()

    def valid(service: str) -> bool | None:
        state = connection_status(store, service)["state"]
        return {"ok": True, "expired": False, "error": False}.get(state)

    return jsonify(status="ok", patreon_session_valid=valid("patreon"),
                   pocketcasts_session_valid=valid("pocketcasts"), youtube_session_valid=valid("youtube"))
