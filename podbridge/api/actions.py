"""Actions (API standard section 7): POST /api/v1/actions/<name> with a JSON body.

Each is one named thing to do, listed in /api/v1/meta with its body. Nothing destructive is
offered. Every call, refused or not, goes in the action log (api_audit), shown on the API tokens
card. A repeat with the same Idempotency-Key within 24 hours, from the same token, gets the first
answer back and does nothing twice; an upstream failure (502) isn't remembered, so it can be
retried with the same key.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import now_iso

IDEMPOTENCY_S = 24 * 3600
KEY_MAX = 100
BODY_MAX = 500
AUDIT_KEEP_DAYS = 90


class ActionError(Exception):
    def __init__(self, code: str, message: str, status: int, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


@dataclass
class Action:
    name: str
    title: str
    body: dict
    run: Callable[[sqlite3.Connection, dict], tuple[str, dict]]   # -> (message, result)
    limit: str | None = None


ACTIONS: dict[str, Action] = {}


def _action(name: str, title: str, body: dict, limit: str | None = None):
    def deco(fn):
        ACTIONS[name] = Action(name, title, body, fn, limit)
        return fn
    return deco


def _episode_id(conn: sqlite3.Connection, body: dict) -> tuple[int, str]:
    v = body.get("episode_id")
    if v is None:
        raise ActionError("bad_request", "Send episode_id, a whole number (from a feed item's meta).", 400)
    if isinstance(v, bool) or not str(v).isdigit():
        raise ActionError("bad_request", "episode_id must be a whole number.", 400)
    row = conn.execute("SELECT id, title FROM episodes WHERE id = ?", (int(v),)).fetchone()
    if row is None:
        raise ActionError("not_found", f"There's no episode {v}.", 404)
    from ..matching import strip_channel_suffix
    return row["id"], strip_channel_suffix(row["title"])


# -- the actions -------------------------------------------------------------------------------------

@_action("sync_now", "Sync now", {}, limit="6/hour")
def _sync_now(conn, body):
    from flask import current_app

    from ..scheduler import run_soon, run_sync_now, restart_countdown
    from ..services import NotConfigured, pocketcasts_configured
    from ..settings_store import get_store
    from ..sync import SyncBusy, _lock
    if _lock.locked():
        raise ActionError("busy", "A sync is already running.", 503, retry_after=30)
    if not pocketcasts_configured(get_store()):
        raise ActionError("forbidden", "Pocket Casts isn't set up yet. Add it in Settings.", 403)

    def go():
        try:
            run_sync_now("manual")
            restart_countdown()
        except (SyncBusy, NotConfigured):
            pass
    backgrounded = run_soon("api-sync", go)
    if not current_app.extensions.get("scheduler") and not backgrounded:
        return "Synced.", {"started": True}
    return "Sync started. sync.finished or sync.failed says how it went.", {"started": True}


def _set_played(conn, body, played: bool):
    from ..services import pocketcasts_client
    from ..settings_store import get_store
    from ..sync import SyncBusy, set_played
    from ..views import POCKETCASTS_FAILURES
    episode_id, title = _episode_id(conn, body)
    try:
        set_played(conn, episode_id, played, pocketcasts_client(get_store()))
    except SyncBusy:
        raise ActionError("busy", "A sync is running right now. Try again in a moment.", 503, retry_after=30) from None
    except ValueError:
        raise ActionError("bad_request", f"{title} isn't matched to a Pocket Casts episode.", 400) from None
    except POCKETCASTS_FAILURES as exc:
        raise ActionError("upstream", f"Pocket Casts didn't take it: {type(exc).__name__}.", 502) from None
    return (f"Marked {title} {'played' if played else 'unplayed'}.",
            {"episode_id": episode_id, "played": played})


@_action("mark_played", "Mark played in Pocket Casts", {"episode_id": "int"})
def _mark_played(conn, body):
    return _set_played(conn, body, True)


@_action("mark_unplayed", "Mark unplayed in Pocket Casts", {"episode_id": "int"})
def _mark_unplayed(conn, body):
    return _set_played(conn, body, False)


@_action("list_add", "Add to My List", {"episode_id": "int"})
def _list_add(conn, body):
    from .. import my_list
    episode_id, title = _episode_id(conn, body)
    if my_list.contains(conn, episode_id):
        return f"{title} is already on My List.", {"episode_id": episode_id, "in_list": True, "changed": False}
    with conn:
        my_list.add(conn, episode_id)
    return f"Added {title} to My List.", {"episode_id": episode_id, "in_list": True, "changed": True}


@_action("list_remove", "Remove from My List", {"episode_id": "int"})
def _list_remove(conn, body):
    from .. import my_list
    episode_id, title = _episode_id(conn, body)
    changed = my_list.contains(conn, episode_id)
    with conn:
        my_list.remove(conn, episode_id)
    return (f"Removed {title} from My List." if changed else f"{title} wasn't on My List."), \
        {"episode_id": episode_id, "in_list": False, "changed": changed}


# -- running one ---------------------------------------------------------------------------------------

def _error(code: str, message: str, status: int) -> tuple[int, dict]:
    return status, {"error": message, "code": code}


def run(conn: sqlite3.Connection, name: str, body: Any, *, token_id: int | None, token_name: str,
        key: str | None = None, over_limit: Callable[[str], Any] | None = None) -> tuple[int, dict, int | None]:
    """Do action `name` once. Returns (HTTP status, JSON body, Retry-After or None)."""
    who = token_id or 0                      # the admin session has one idempotency space
    if key is not None and not (0 < len(key) <= KEY_MAX):
        return (*_error("bad_request", f"Idempotency-Key must be 1 to {KEY_MAX} characters.", 400), None)
    if key is not None:
        row = conn.execute("SELECT action, status, body, created_at FROM api_idempotency "
                           "WHERE token_id = ? AND key = ?", (who, key)).fetchone()
        if row is not None and row["created_at"] >= now_iso(-IDEMPOTENCY_S):
            if row["action"] != name:
                return (*_error("bad_request", "That Idempotency-Key was already used for a different action.", 400),
                        None)
            return row["status"], json.loads(row["body"]), None

    retry_after = None
    action = ACTIONS.get(name)
    if action is None:
        status, out = _error("not_found", f"There's no action called '{name}'. Actions: "
                             + ", ".join(ACTIONS) + ".", 404)
    elif not isinstance(body, dict):
        status, out = _error("bad_request", "Send a JSON object as the body ({} if there's nothing to send).", 400)
    else:
        refused = over_limit(name) if (over_limit and action.limit) else None
        if refused is not None:
            out = refused.get_json()
            status, retry_after = refused.status_code, int(refused.headers.get("Retry-After", "60"))
        else:
            try:
                message, result = action.run(conn, body)
                status, out = 200, {"ok": True, "message": message, "result": result}
            except ActionError as exc:
                status, out = _error(exc.code, exc.message, exc.status)
                retry_after = exc.retry_after

    raw = json.dumps(body if isinstance(body, dict) else {}, separators=(",", ":"), sort_keys=True)
    with conn:
        conn.execute("INSERT INTO api_audit (at, token_id, token_name, action, body, status, message) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (now_iso(), token_id, token_name, name, raw[:BODY_MAX], status,
                                                      out.get("message") or out.get("error")))
        if key is not None and status < 500:
            conn.execute("INSERT OR REPLACE INTO api_idempotency (token_id, key, action, status, body, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (who, key, name, status, json.dumps(out), now_iso()))
    return status, out, retry_after


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT at, token_name, action, status, message FROM api_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,))]


def prune(conn: sqlite3.Connection) -> int:
    with conn:
        n = conn.execute("DELETE FROM api_idempotency WHERE created_at < ?", (now_iso(-IDEMPOTENCY_S),)).rowcount
        return n + conn.execute("DELETE FROM api_audit WHERE at < ?", (now_iso(-AUDIT_KEEP_DAYS * 86400),)).rowcount
