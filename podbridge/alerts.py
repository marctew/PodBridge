"""Problems worth a banner on every page (spec section 9, banner only: no notifications).

Worked out fresh on each page load from state PodBridge already keeps, so a banner appears
as soon as something breaks and disappears as soon as it's fixed; there's nothing to dismiss.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .settings_store import SettingsStore

FAILED_RUNS_THRESHOLD = 3
SERVICES = {"patreon": "Patreon", "pocketcasts": "Pocket Casts", "youtube": "YouTube"}
LOGIN_SECRETS = {
    "patreon": ("patreon_session_id",),
    "pocketcasts": ("pocketcasts_email", "pocketcasts_password"),
    "youtube": ("youtube_cookies",),
}


@dataclass(frozen=True)
class Problem:
    key: str           # 'login:<service>' or 'sync'
    text: str
    since: str | None  # ISO timestamp, shown as "since …"
    action: str        # link text
    target: str        # 'settings#<service>' or 'activity'


def _uses_youtube(conn: sqlite3.Connection, store: SettingsStore) -> bool:
    return store.is_set("youtube_cookies") or conn.execute(
        "SELECT 1 FROM sources WHERE enabled = 1 AND kind = 'youtube'").fetchone() is not None


def current_problems(conn: sqlite3.Connection, store: SettingsStore) -> list[Problem]:
    problems: list[Problem] = []

    for service, label in SERVICES.items():
        if not all(store.is_set(k) for k in LOGIN_SECRETS[service]):
            continue  # not set up: the dashboard already says so
        if service == "youtube" and not _uses_youtube(conn, store):
            continue
        state = store.get(f"{service}_status")
        if state == "expired":
            since = (store.get("youtube_expired_at") if service == "youtube" else None) \
                or store.get(f"{service}_verified_at")
            text = {"patreon": "Patreon session has expired: syncing from Patreon has stopped.",
                    "pocketcasts": "Pocket Casts login has expired.",
                    "youtube": "YouTube login has expired: YouTube progress isn't being read."}[service]
            problems.append(Problem(f"login:{service}", text, since,
                                    "Paste a fresh cookie" if service == "patreon"
                                    else "Export fresh cookies" if service == "youtube" else "Fix it",
                                    f"settings#{service}"))
        elif state == "error":
            problems.append(Problem(f"login:{service}", f"{label} isn't working.",
                                    store.get(f"{service}_verified_at"), "Check settings", f"settings#{service}"))

    runs = conn.execute("SELECT status, error, started_at FROM sync_runs WHERE status != 'running' "
                        "ORDER BY id DESC LIMIT ?", (FAILED_RUNS_THRESHOLD,)).fetchall()
    failing = len(runs) == FAILED_RUNS_THRESHOLD and all(r["status"] in ("error", "aborted") for r in runs)
    # Runs aborted only because a login expired are already covered by that login's banner.
    if failing and any(r["status"] == "error" for r in runs):
        latest = runs[0]["error"] or "unknown error"
        problems.append(Problem("sync", f"The last {FAILED_RUNS_THRESHOLD} syncs failed: {latest[:160]}",
                                runs[-1]["started_at"], "See Activity", "activity"))
    return problems
