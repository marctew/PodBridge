"""In-process scheduler (APScheduler) with a single interval job.

A manual sync, or a change to the interval setting, restarts the countdown so
the next automatic run is one full interval later.
"""

from __future__ import annotations

import atexit
import logging
from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, current_app

from .crypto import SecretError
from .db import get_db
from .services import NotConfigured, patreon_client, pocketcasts_client
from .settings_store import get_store
from .sync import RunSummary, SyncBusy, run_sync

log = logging.getLogger(__name__)

JOB_ID = "sync"


def run_sync_now(tier: str) -> RunSummary:
    """Build clients from stored credentials and run a sync. Needs an app context.
    Raises NotConfigured, SecretError or SyncBusy."""
    store = get_store()
    return run_sync(get_db(), store, patreon_client(store), pocketcasts_client(store), tier)


def _scheduled_job(app: Flask) -> None:
    with app.app_context():
        try:
            run_sync_now("full")
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
