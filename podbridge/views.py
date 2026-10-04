"""Web UI routes and /healthz."""

from __future__ import annotations

import io
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session,
    url_for,
)

from .resume import parse_time

from .alerts import current_problems

from .auth import safe_next
from .crypto import SecretError
from .db import get_db, utcnow
from .discovery import check_patreon_session, discover_all
from .http import TransportError
from .linking import (
    MatchError, allow_auto_match, apply_pocketcasts_state, check_pocketcasts_login, link_source, refresh_all,
    set_manual_match, source_podcasts, take_over_match, unlink, widen_source,
)
from .patreon import PatreonBlocked, PatreonError, PatreonSessionExpired
from .pocketcasts import PocketCastsAuthError, PocketCastsBlocked, PocketCastsError
from .matching import strip_channel_suffix, title_similarity
from . import artwork, backup, catchup, hide_rules, history, library, stats
from .library import EPISODE_QUERY, annotate
from .resume import TIMESTAMP_PARAM_PATTERN
from .scheduler import (
    backfill_status, next_run_at, restart_countdown, run_date_backfill, run_soon, run_sync_now, scheduler_running,
    start_date_backfill,
)
from .sync import SyncBusy, set_played, sync_one_episode
from .sync import _lock as sync_lock
from .services import (
    NotConfigured, art_dir, patreon_client, pocketcasts_client, pocketcasts_configured, pocketcasts_tokens,
    youtube_client,
)
from .discovery import (
    check_youtube_session, discover_youtube_all, has_sources, missing_youtube_dates, note_youtube_expiry,
)
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
        note_youtube_expiry(store)
        return f"YouTube login has expired: {exc}. Export fresh cookies and paste them in Settings."
    if isinstance(exc, YouTubeBlocked):
        return f"{exc}: YouTube is pushing back, so try again later."
    store.set("youtube_status", "error")
    return f"YouTube problem: {exc}"


# --- resume links ---

@bp.app_context_processor
def inject_problems():
    """Banner problems for every signed-in page."""
    if not session.get("auth"):
        return {}
    try:
        found = current_problems(get_db(), get_store())
    except SecretError:
        found = []
    banners = []
    for p in found:
        page, _, anchor = p.target.partition("#")
        link = url_for("main.settings", _anchor=anchor or None) if page == "settings" else url_for("main.activity")
        banners.append({"text": p.text, "since": p.since, "action": p.action, "link": link})
    return {"problems": banners}


VISIT_GAP = timedelta(minutes=30)


def new_since() -> datetime | None:
    """Start of your previous visit: episodes first seen after it are "new".
    A visit ends after 30 minutes without opening a page. Only page views (GET) count."""
    if "new_since" in g:
        return g.new_since
    store = get_store()
    now = datetime.now(timezone.utc)
    last = parse_time(store.get("last_visit_at"))
    previous = parse_time(store.get("previous_visit_at"))
    if request.method == "GET":
        if last is None:
            previous = now  # first visit ever: nothing is "new" yet
            store.set("previous_visit_at", utcnow())
        elif now - last > VISIT_GAP:
            previous = last
            store.set("previous_visit_at", store.get("last_visit_at"))
        store.set("last_visit_at", utcnow())
    g.new_since = previous
    return previous


def shows() -> list[library.Show]:
    return library.build_shows(get_db(), get_store().get("patreon_timestamp_param"), new_since())


def recently_watched(all_shows: list[library.Show], limit: int = 12) -> list[dict]:
    by_id = {e["id"]: e for e in library.all_episodes(all_shows)}
    return [by_id[i] for i in history.recently_watched_ids(get_db(), limit) if i in by_id]


def continue_watching(limit: int | None = 12) -> list[dict]:
    return library.continue_watching(shows(), limit)


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
            if row is None or not (row["pocketcasts_episode_uuid"] and row["matched_podcast_uuid"]):
                return
            state = client.get_episode_state(row["pocketcasts_episode_uuid"], row["matched_podcast_uuid"])
            if state:
                with db:
                    apply_pocketcasts_state(db, episode_id, state.status, state.played_up_to)
            return
        podcasts = [r[0] for r in db.execute(
            "SELECT DISTINCT sp.podcast_uuid FROM source_podcasts sp JOIN sources s ON s.id = sp.source_id "
            "WHERE s.enabled = 1")]
        for podcast_uuid in podcasts:
            states = client.episode_states(podcast_uuid)
            matched = db.execute(
                "SELECT e.id, e.pocketcasts_episode_uuid FROM episodes e "
                "JOIN pocketcasts_episodes pe ON pe.uuid = e.pocketcasts_episode_uuid "
                "WHERE pe.podcast_uuid = ?", (podcast_uuid,)).fetchall()
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
    all_shows = shows()
    last_run = db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM sources WHERE enabled = 1) AS sources, "
        "(SELECT COUNT(*) FROM episodes) AS episodes, "
        "(SELECT COUNT(*) FROM episodes WHERE match_method != 'none') AS matched, "
        "(SELECT COUNT(*) FROM episodes e JOIN sources s ON s.id = e.source_id "
        " WHERE e.match_method = 'none' AND e.match_locked = 0 AND e.hidden = 0 "
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
        watching=library.continue_watching(all_shows),
        up_next=library.up_next(all_shows),
        new_week=library.new_this_week(all_shows),
        recently_watched=recently_watched(all_shows),
        recent=library.recently_added(all_shows),
    )


@bp.get("/library")
def library_page():
    all_shows = shows()
    query = request.args.get("q", "").strip()
    found_shows, found_episodes = library.search(all_shows, query) if query else ([], [])
    return render_template("library.html", shows=all_shows, query=query,
                           found_shows=found_shows, found_episodes=found_episodes)


@bp.get("/library/<int:source_id>/<slug>")
def show_page(source_id: int, slug: str):
    podcast_uuid = None if slug == "-" else slug
    show = next((s for s in shows() if s.source_id == source_id and s.podcast_uuid == podcast_uuid), None)
    if show is None:
        abort(404)
    current = request.args.get("filter", "all")
    filters = {"all": lambda e: True, "in_progress": lambda e: e["state"] == "in_progress",
               "unwatched": lambda e: e["state"] == "unwatched", "played": lambda e: e["state"] == "played",
               "hidden": None}
    if current == "hidden":
        episodes = show.hidden_episodes
    else:
        episodes = [e for e in show.episodes if (filters.get(current) or filters["all"])(e)]
    up_next = library.continue_watching([show], limit=1)
    unmatched = sum(1 for e in show.episodes if e["match_method"] == "none" and not e["match_locked"])
    batch = catchup.latest_for_scope(get_db(), f"{source_id}/{slug}")
    return render_template("show.html", show=show, episodes=episodes, current=current, filters=list(filters),
                           up_next=up_next[0] if up_next else None, unmatched=unmatched, batch=batch)


@bp.post("/episodes/<int:episode_id>/hide")
def hide_episode(episode_id: int):
    hidden = request.form.get("hidden", "1") == "1"
    db = get_db()
    with db:
        if hidden:
            db.execute("UPDATE episodes SET hidden = 1, hidden_by_rule = NULL WHERE id = ?", (episode_id,))
        else:
            # Unhiding by hand wins over auto-hide rules from now on.
            db.execute("UPDATE episodes SET hidden = 0, hidden_by_rule = NULL, hide_override = 1 WHERE id = ?",
                       (episode_id,))
    flash("Hidden. Find it again under the Hidden filter." if hidden else "Unhidden.", "ok")
    return redirect(safe_next(request.form.get("next") or url_for("main.library_page")))


@bp.post("/library/<int:source_id>/<slug>/hide-unmatched")
def hide_unmatched(source_id: int, slug: str):
    show = next((s for s in shows() if s.source_id == source_id and s.slug == slug), None)
    if show is None:
        abort(404)
    ids = [e["id"] for e in show.episodes if e["match_method"] == "none" and not e["match_locked"]]
    db = get_db()
    with db:
        db.executemany("UPDATE episodes SET hidden = 1, hidden_by_rule = NULL WHERE id = ?", [(i,) for i in ids])
    flash(f"Hid {len(ids)} unmatched episode{'' if len(ids) == 1 else 's'}. They're under the Hidden filter.", "ok")
    return redirect(url_for("main.show_page", source_id=source_id, slug=slug))


@bp.post("/episodes/<int:episode_id>/catch-up")
def catch_up(episode_id: int):
    """Mark this episode and everything older in its show as played (in the background)."""
    target = next((e for s in shows() for e in s.episodes if e["id"] == episode_id), None)
    if target is None:
        abort(404)
    show = next(s for s in shows() if f"{s.source_id}/{s.slug}" == target["show_scope"])
    cutoff = target["published_at"] or ""
    ids = [e["id"] for e in show.episodes
           if e["state"] != "played" and (e["published_at"] or "") <= cutoff]
    back = url_for("main.show_page", source_id=show.source_id, slug=show.slug)
    if not ids:
        flash("Everything up to there is already played.", "ok")
        return redirect(back)
    db = get_db()
    batch_id = catchup.create_batch(db, ids, f"“{target['title']}” and everything older", target["show_scope"])
    pc = None
    try:
        pc = pocketcasts_client(get_store())
    except (NotConfigured, SecretError):
        pass  # no Pocket Casts: mark played inside PodBridge only
    backgrounded = run_soon(f"catch-up-{batch_id}", _run_catch_up, batch_id, pc)
    flash(f"Marking {len(ids)} episode{'' if len(ids) == 1 else 's'} played"
          + (" in the background. Refresh to see progress." if backgrounded else "."), "ok")
    return redirect(back)


def _run_catch_up(batch_id: int, pc) -> None:
    catchup.run_batch(get_db(), batch_id, pc)


def _undo_catch_up(batch_id: int, pc) -> None:
    catchup.undo_batch(get_db(), batch_id, pc)


@bp.post("/catch-up/<int:batch_id>/undo")
def undo_catch_up(batch_id: int):
    batch = get_db().execute("SELECT * FROM catch_up_batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        abort(404)
    pc = None
    try:
        pc = pocketcasts_client(get_store())
    except (NotConfigured, SecretError):
        pass
    backgrounded = run_soon(f"catch-up-undo-{batch_id}", _undo_catch_up, batch_id, pc)
    flash("Undoing" + (" in the background. Refresh to see progress." if backgrounded else ": done."), "ok")
    source_id, _, slug = batch["scope"].partition("/")
    return redirect(url_for("main.show_page", source_id=int(source_id), slug=slug))


@bp.get("/settings/backup")
def download_backup():
    data = backup.make_backup(get_db())
    name = f"podbridge-backup-{datetime.now().strftime('%Y-%m-%d-%H%M')}.db"
    return send_file(io.BytesIO(data), mimetype="application/vnd.sqlite3", as_attachment=True, download_name=name)


@bp.post("/settings/restore")
def restore_backup():
    upload = request.files.get("backup")
    if upload is None or not upload.filename:
        flash("Choose a backup file to restore.", "error")
        return redirect(url_for("main.settings", _anchor="backup"))
    drop = bool(request.form.get("without_credentials"))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "restore.db"
        upload.save(path)
        try:
            with sync_lock:  # no sync writing while the database is swapped
                info = backup.restore(get_db(), path, current_app.extensions["secret_box"], drop_secrets=drop)
        except backup.BackupError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.settings", _anchor="backup"))
    pocketcasts_tokens().clear()
    restart_countdown()
    flash(f"Restored: {info.sources} sources and {info.episodes} episodes"
          + (". Credentials were left out, so re-enter them below." if drop and info.has_secrets else "."), "ok")
    return redirect(url_for("main.settings"))


@bp.get("/stats")
def stats_page():
    titles = {e["id"]: s.title for s in shows() for e in (*s.episodes, *s.hidden_episodes)}
    summary = stats.summarize(get_db(), current_app.config["PODBRIDGE"].tz, titles)
    return render_template("stats.html", s=summary, sides=stats.SIDES, side_labels=stats.SIDE_LABELS)


@bp.get("/history")
def history_page():
    days = history.by_day(get_db(), current_app.config["PODBRIDGE"].tz)
    return render_template("history.html", days=days)


@bp.get("/art/<key>")
def art(key: str):
    """Cached artwork; YouTube thumbnails and podcast art are fetched on first request."""
    if not artwork.KEY_PATTERN.match(key):
        abort(404)
    path = artwork.ensure(get_db(), art_dir(), key) if not key.startswith("patreon:") else None
    if path is None:
        row = artwork.cached(get_db(), key)
        path = art_dir() / row["filename"] if row is not None and row["ok"] else None
    if path is None or not path.is_file():
        abort(404)
    return send_file(path, max_age=7 * 86400)


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


@bp.post("/episodes/<int:episode_id>/sync")
def sync_episode(episode_id: int):
    """Sync one episode now: fresh progress from its source and from Pocket Casts."""
    store = get_store()
    back = safe_next(request.form.get("next") or url_for("main.library_page"))
    try:
        pc = pocketcasts_client(store)
        kind = get_db().execute("SELECT s.kind FROM episodes e JOIN sources s ON s.id = e.source_id "
                                "WHERE e.id = ?", (episode_id,)).fetchone()
        if kind is None:
            abort(404)
        clients = {"patreon": None, "youtube": None}
        try:
            clients[kind[0]] = patreon_client(store) if kind[0] == "patreon" else youtube_client(store)
        except (NotConfigured, YouTubeError) as exc:
            flash(str(exc), "error")
            return redirect(back)
        summary = sync_one_episode(get_db(), store, episode_id, pc, **clients)
    except SyncBusy:
        flash("A sync is running right now. Try again in a moment.", "warn")
        return redirect(back)
    except (NotConfigured, SecretError) as exc:
        flash(str(exc) if isinstance(exc, NotConfigured) else "Stored credentials can't be decrypted.", "error")
        return redirect(back)
    if summary.status != "ok":
        flash(f"Sync {summary.status}: {summary.error}", "error")
    else:
        event = get_db().execute("SELECT detail FROM sync_events WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                                 (summary.run_id,)).fetchone()
        flash(event["detail"] if event else "Nothing to sync: no progress on the source side yet.", "ok")
    return redirect(back)


@bp.post("/episodes/<int:episode_id>/played")
def set_played_route(episode_id: int):
    store = get_store()
    back = safe_next(request.form.get("next") or url_for("main.library_page"))
    played = request.form.get("played") == "1"
    try:
        flash(set_played(get_db(), episode_id, played, pocketcasts_client(store)) + ".", "ok")
    except SyncBusy:
        flash("A sync is running right now. Try again in a moment.", "warn")
    except ValueError as exc:
        flash(str(exc), "error")
    except POCKETCASTS_FAILURES as exc:
        flash(pocketcasts_failure(store, exc), "error")
    return redirect(back)


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

    secrets_state = {key: store.updated_at(key) for key in SECRET_FIELDS}
    return render_template(
        "settings.html",
        secrets_state=secrets_state,
        patreon=connection_status(store, "patreon"),
        pocketcasts=connection_status(store, "pocketcasts"),
        youtube=connection_status(store, "youtube"),
        youtube_login=youtube_login_lifetime(store),
        interval=store.get_int("sync_interval_minutes"),
        dry_run=store.get_bool("dry_run"),
        timestamp_param=store.get("patreon_timestamp_param"),
        min_interval=MIN_INTERVAL_MINUTES,
        max_interval=MAX_INTERVAL_MINUTES,
    )


def youtube_login_lifetime(store: SettingsStore) -> dict | None:
    """When the current YouTube login was pasted, when it stopped working, and how long it lasted."""
    started = store.get("youtube_login_started_at")
    if not started:
        return None
    expired = store.get("youtube_expired_at")
    end = datetime.fromisoformat((expired or utcnow()).replace("Z", "+00:00"))
    hours = (end - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() / 3600
    if hours < 1:
        span = f"{round(hours * 60)} minutes"
    elif hours < 48:
        span = f"{hours:.1f} hours"
    else:
        span = f"{hours / 24:.1f} days"
    return {"started": started, "expired": expired, "span": span}


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
        store.set_secret(key, value)
        if service:
            changed.add(service)

    for service in changed:
        store.set(f"{service}_status", "unknown")
        store.delete(f"{service}_verified_at")
        if service == "pocketcasts":
            store.delete("pocketcasts_refresh_token")
            pocketcasts_tokens().clear()
        if service == "youtube":
            # A fresh login: start timing how long it lasts.
            store.set("youtube_login_started_at", utcnow())
            store.delete("youtube_expired_at")

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
    "all": "e.hidden = 0",
    "in_progress": "e.hidden = 0 AND p.patreon_watch_state = 'is_watching' AND COALESCE(p.patreon_is_watched, 0) = 0",
    "watched": "e.hidden = 0 AND p.patreon_is_watched = 1",
    "unmatched": "e.hidden = 0 AND e.match_method = 'none' AND e.match_locked = 0",
    "hidden": "e.hidden = 1",
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
            for r in discover_all(db, store, patreon_client(store), art_dir()):
                flash(f"Patreon · {r.label}: {r.posts_seen} posts, {r.added} new"
                      + (f", {r.skipped_no_media} without media skipped" if r.skipped_no_media else ""), "ok")
        except PATREON_FAILURES as exc:
            flash(patreon_failure(store, exc), "error")
    if has_sources(db, "youtube"):
        try:
            for r in discover_youtube_all(db, store, youtube_client(store)):
                flash(f"YouTube · {r.label}: {r.posts_seen} recent uploads, {r.added} new", "ok")
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

    caught = hide_rules.apply_rules(db)
    if caught:
        flash(f"Auto-hide rules hid {caught} new episode{'' if caught == 1 else 's'}.", "ok")
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
    rows = db.execute(
        "SELECT pe.*, sp.title AS podcast_title, other.id AS taken_by_id, other.title AS taken_by_title "
        "FROM pocketcasts_episodes pe JOIN source_podcasts sp ON sp.podcast_uuid = pe.podcast_uuid "
        "LEFT JOIN (SELECT e.id, e.title, e.pocketcasts_episode_uuid FROM episodes e "
        "           JOIN sources s ON s.id = e.source_id WHERE s.enabled = 1) other "
        "  ON other.pocketcasts_episode_uuid = pe.uuid AND other.id != ? "
        "WHERE sp.source_id = ?", (episode_id, episode["source_id"])).fetchall()
    title = strip_channel_suffix(episode["title"])

    def closeness(r) -> tuple:
        length_gap = (abs(r["duration_secs"] - episode["duration_secs"])
                      if r["duration_secs"] and episode["duration_secs"] else float("inf"))
        if episode["published_at"]:
            return (_days_apart(r["published_at"], episode["published_at"]), -title_similarity(title, r["title"]))
        # No date yet (a YouTube video whose details haven't been fetched): most similar title first.
        return (-round(title_similarity(title, r["title"]), 2), length_gap)

    candidates = sorted(rows, key=lambda r: (r["taken_by_id"] is not None, closeness(r)))
    several = len({r["podcast_uuid"] for r in rows}) > 1
    back = safe_next(request.args.get("next") or url_for("main.episodes"))
    return render_template("match.html", episode=episode, candidates=candidates, several_podcasts=several,
                           back=back)


@bp.post("/episodes/<int:episode_id>/match")
def save_match(episode_id: int):
    """Returns to the list the match page was opened from (e.g. the Unmatched filter)."""
    db = get_db()
    back = safe_next(request.form.get("next") or url_for("main.episodes"))
    try:
        if request.form.get("action") == "unlink":
            unlink(db, episode_id)
            flash("Unlinked. Auto-matching will leave this episode alone.", "ok")
        elif request.form.get("action") == "allow_auto":
            allow_auto_match(db, episode_id)
            flash("Auto-matching re-enabled for this episode; it applies on the next refresh.", "ok")
        elif request.form.get("action") == "take_over":
            previous = take_over_match(db, episode_id, request.form.get("uuid", ""))
            flash(f"Matched here, and unmatched “{previous}”." if previous else "Matched.", "ok")
        else:
            set_manual_match(db, episode_id, request.form.get("uuid", ""))
            flash("Matched.", "ok")
    except MatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.match_episode", episode_id=episode_id, next=back))
    return redirect(back)


# --- sources ---

@bp.get("/sources")
def sources():
    db = get_db()
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM episodes e WHERE e.source_id = s.id) AS episode_count, "
        "(SELECT COUNT(*) FROM hide_rules r WHERE r.source_id = s.id) AS rule_count, "
        "(SELECT GROUP_CONCAT(COALESCE(sp.title, 'Linked'), ' + ') FROM source_podcasts sp "
        " WHERE sp.source_id = s.id) AS podcast_names, "
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
    return render_template("sources.html", sources=rows, campaign_id=campaign_id, collections=collections,
                           missing_dates=missing_youtube_dates(db), backfill=backfill_status())


@bp.post("/sources/youtube/dates")
def fetch_youtube_dates():
    """Fill in every missing YouTube publish date in the background, then re-match."""
    if backfill_status().get("running"):
        flash("Already fetching YouTube dates.", "warn")
    elif start_date_backfill():
        flash("Fetching YouTube publish dates in the background (about 1.5 s each). "
              "Matches update when it finishes; refresh this page to see progress.", "ok")
    else:
        filled, matched = run_date_backfill()  # no scheduler (local dev): run inline
        flash(f"Filled in {filled} YouTube dates; {matched} new matches.", "ok")
    return redirect(url_for("main.sources"))


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


@bp.route("/sources/<int:source_id>/rules", methods=["GET", "POST"])
def source_rules(source_id: int):
    db = get_db()
    source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        abort(404)
    if request.method == "POST":
        if request.form.get("delete"):
            rule_id = int(request.form["delete"])
            if rule_id not in {r.id for r in hide_rules.rules_for(db, source_id)}:
                abort(404)
            unhidden = hide_rules.delete_rule(db, rule_id)
            flash(f"Rule deleted; {unhidden} episode{'' if unhidden == 1 else 's'} unhidden.", "ok")
        else:
            try:
                rule, caught = hide_rules.add_rule(db, source_id, request.form.get("kind", ""),
                                                   request.form.get("value", ""))
            except hide_rules.RuleError as exc:
                flash(str(exc), "error")
            else:
                flash(f"Rule added ({rule.describe()}): hid {caught} episode{'' if caught == 1 else 's'}.", "ok")
        return redirect(url_for("main.source_rules", source_id=source_id))
    return render_template("rules.html", source=source, rules=hide_rules.rules_for(db, source_id),
                           counts=hide_rules.caught_counts(db), kinds=hide_rules.KINDS)


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
    linked = source_podcasts(get_db(), source_id)
    podcasts.sort(key=lambda p: p.uuid not in linked)  # stable: currently linked ones stay on top
    return render_template("link_source.html", source=source, podcasts=podcasts, linked=linked)


@bp.post("/sources/<int:source_id>/link")
def link_source_save(source_id: int):
    uuids = [u.strip() for u in request.form.getlist("podcast_uuid") if u.strip()]
    if not uuids:
        flash("Tick at least one podcast.", "error")
        return redirect(url_for("main.link_source_form", source_id=source_id))
    podcasts = [(u, request.form.get(f"title_{u}") or None) for u in dict.fromkeys(uuids)]
    try:
        link_source(get_db(), source_id, podcasts)
    except MatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.sources"))
    names = ", ".join(t or u[:8] for u, t in podcasts)
    flash(f"Linked to {names}. Use “Refresh” on the Episodes page to load and match episodes.", "ok")
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
