"""In-process scheduler (APScheduler) with a single interval job.

A manual sync, or a change to the interval setting, restarts the countdown so
the next automatic run is one full interval later.
"""

from __future__ import annotations

import atexit
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, current_app

from .crypto import SecretError
from .db import get_db
from .discovery import BACKGROUND_DETAIL_FETCHES, backfill_youtube_dates
from .http import TransportError
from .linking import rematch_youtube_offline
from .services import (
    NotConfigured, art_dir, patreon_client, pocketcasts_client, youtube_client, youtube_public_client,
)
from .youtube import YouTubeError
from .settings_store import get_store
from .sync import RunSummary, SyncBusy, run_sync

log = logging.getLogger(__name__)

JOB_ID = "sync"


BACKFILL_JOB_ID = "youtube-dates"


def run_date_backfill() -> tuple[int, int]:
    """Fetch every missing YouTube publish date (public pages, no login), then re-match YouTube
    sources against the cached Pocket Casts catalogue. Needs an app context.
    Returns (dates filled, new matches)."""
    status = current_app.extensions.setdefault("youtube_backfill", {})
    status.update(running=True, filled=0, matched=0, error=None)
    try:
        db = get_db()
        filled = backfill_youtube_dates(db, youtube_public_client())
        matched = rematch_youtube_offline(db)
        status.update(filled=filled, matched=matched)
        log.info("YouTube date backfill: %s dates filled, %s new matches", filled, matched)
        return filled, matched
    except (YouTubeError, TransportError) as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("YouTube date backfill stopped: %s", status["error"])
        return status["filled"], 0
    finally:
        status["running"] = False


def _backfill_job(app: Flask) -> None:
    with app.app_context():
        run_date_backfill()


def start_date_backfill() -> bool:
    """Run the backfill in the background. Returns False if there's no scheduler (dev/tests),
    in which case the caller should run it inline."""
    scheduler = current_app.extensions.get("scheduler")
    if scheduler is None:
        return False
    current_app.extensions.setdefault("youtube_backfill", {})["running"] = True
    scheduler.add_job(_backfill_job, args=[current_app._get_current_object()], id=BACKFILL_JOB_ID,
                      replace_existing=True, max_instances=1, next_run_time=datetime.now(timezone.utc))
    return True


def backfill_status() -> dict:
    return current_app.extensions.get("youtube_backfill", {})


def run_sync_now(tier: str, youtube_detail_budget: int | None = None) -> RunSummary:
    """Build clients from stored credentials and run a sync. Needs an app context.
    Pocket Casts is required (raises NotConfigured); a missing or unusable Patreon or
    YouTube login only affects that kind's sources. Raises SecretError or SyncBusy too."""
    store = get_store()
    pocketcasts = pocketcasts_client(store)
    clients: dict[str, object] = {}
    unavailable: dict[str, str] = {}
    for kind, build in (("patreon", patreon_client), ("youtube", youtube_client)):
        try:
            clients[kind] = build(store)
        except (NotConfigured, SecretError, YouTubeError) as exc:
            unavailable[kind] = str(exc)
    return run_sync(get_db(), store, clients.get("patreon"), pocketcasts, tier,
                    youtube=clients.get("youtube"), unavailable=unavailable, art_dir=art_dir(),
                    youtube_detail_budget=youtube_detail_budget)


def _scheduled_job(app: Flask) -> None:
    with app.app_context():
        try:
            # Nobody waits on a scheduled run, so it fetches every missing YouTube date.
            run_sync_now("full", youtube_detail_budget=BACKGROUND_DETAIL_FETCHES)
        except NotConfigured as exc:
            log.info("Scheduled sync skipped: %s", exc)
        except SyncBusy:
            log.info("Scheduled sync skipped: a sync is already running")
        except SecretError as exc:
            log.warning("Scheduled sync skipped: %s", exc)


def init_scheduler(app: Flask) -> None:
    if not app.config["PODBRIDGE"].scheduler_enabled:
        return
    with app.app_context():
        minutes = get_store().get_int("sync_interval_minutes")
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_scheduled_job, IntervalTrigger(minutes=minutes), args=[app], id=JOB_ID,
                      max_instances=1, coalesce=True, misfire_grace_time=300)
    scheduler.start()
    app.extensions["scheduler"] = scheduler
    atexit.register(lambda: scheduler.running and scheduler.shutdown(wait=False))
    log.info("Scheduler started: sync every %s minutes", minutes)


def restart_countdown(minutes: int | None = None) -> None:
    """Next automatic run = now + interval."""
    scheduler = current_app.extensions.get("scheduler")
    if scheduler is None:
        return
    if minutes is None:
        minutes = get_store().get_int("sync_interval_minutes")
    scheduler.reschedule_job(JOB_ID, trigger=IntervalTrigger(minutes=minutes))


def next_run_at() -> str | None:
    scheduler = current_app.extensions.get("scheduler")
    job = scheduler.get_job(JOB_ID) if scheduler else None
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scheduler_running() -> bool:
    return current_app.extensions.get("scheduler") is not None
