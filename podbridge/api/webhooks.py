"""Webhooks out (API standard section 8), for Home Assistant and n8n.

Settings → Webhooks holds a URL, the event types to send (none ticked: all) and a secret. Each
event makes one delivery per matching webhook; the scheduler's housekeeping job POSTs it as JSON
(the same `data` as the events stream) with `X-Signature: sha256=<hex HMAC-SHA256 of the body>`,
a 10 s timeout, and retries after 1, 3 and 6 minutes before giving up. Deliveries never happen in
a request. The URL is a secret too (a Home Assistant webhook id is the whole credential), so once
saved it's only ever shown masked.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .. import __version__
from ..du_api import RETRY_AFTER_S, WEBHOOK_TIMEOUT_S, sign
from . import now_iso, parse

KEEP_DONE_DAYS = 7
BATCH = 20


def _types() -> list[str]:
    from .events import TYPES
    return TYPES


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Paste a full http:// or https:// URL, e.g. http://homeassistant.lan:8123/api/webhook/podbridge")
    return url


def mask(url: str) -> str:
    """'http://homeassistant.lan:8123/api/webhook/abc123' -> 'http://homeassistant.lan:8123/•••'."""
    p = urlsplit(url or "")
    return f"{p.scheme}://{p.netloc}/•••" if p.netloc else "•••"


def create(conn: sqlite3.Connection, name: str, url: str, event_types: list[str] | None = None) -> str:
    """Save a webhook; returns its signing secret, which is shown once."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the webhook a name, e.g. Home Assistant.")
    url = validate_url(url)
    types = [t for t in (event_types or []) if t in _types()]
    secret = secrets.token_urlsafe(32)
    with conn:
        conn.execute("INSERT INTO webhooks (name, url, secret, event_types, enabled, created_at) "
                     "VALUES (?, ?, ?, ?, 1, ?)", (name, url, secret, json.dumps(types), now_iso()))
    return secret


def new_secret(conn: sqlite3.Connection, webhook_id: int) -> str | None:
    secret = secrets.token_urlsafe(32)
    with conn:
        if not conn.execute("UPDATE webhooks SET secret = ? WHERE id = ?", (secret, webhook_id)).rowcount:
            return None
    return secret


def delete(conn: sqlite3.Connection, webhook_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,)).rowcount > 0


def name_of(conn: sqlite3.Connection, webhook_id: int) -> str | None:
    row = conn.execute("SELECT name FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    return row[0] if row else None


def _ago(value: str | None) -> str:
    moment = parse(value)
    if moment is None:
        return ""
    secs = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)}m ago"
    if secs < 129600:
        return f"{round(secs / 3600)}h ago"
    return f"{round(secs / 86400)}d ago"


def list_webhooks(conn: sqlite3.Connection) -> list[dict]:
    """For the Webhooks card: never the URL or the secret in full."""
    out = []
    for r in conn.execute("SELECT * FROM webhooks ORDER BY created_at, id"):
        pending = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND done_at IS NULL",
                               (r["id"],)).fetchone()[0]
        failed = bool(r["last_error_at"] and (not r["last_ok_at"] or r["last_error_at"] > r["last_ok_at"]))
        if failed:
            status = f"failed {_ago(r['last_error_at'])}: {r['last_error']}"
        elif r["last_ok_at"]:
            status = f"sent {_ago(r['last_ok_at'])}"
        else:
            status = "nothing sent yet"
        out.append({"id": r["id"], "name": r["name"], "masked_url": mask(r["url"]),
                    "event_types": json.loads(r["event_types"] or "[]"), "status": status, "failed": failed,
                    "pending": pending})
    return out


def enqueue(conn: sqlite3.Connection, event_id: int, type_: str) -> int:
    """One delivery per enabled webhook that wants this type. Called inside publish()'s transaction."""
    n = 0
    for r in conn.execute("SELECT id, event_types FROM webhooks WHERE enabled = 1").fetchall():
        wanted = json.loads(r["event_types"] or "[]")
        if not wanted or type_ in wanted:
            conn.execute("INSERT INTO webhook_deliveries (webhook_id, event_id, attempts, next_at) VALUES (?, ?, 0, ?)",
                         (r["id"], event_id, now_iso()))
            n += 1
    return n


def _post(url: str, body: bytes, headers: dict) -> None:
    import requests
    r = requests.post(url, data=body, headers={**headers, "User-Agent": f"PodBridge/{__version__}"},
                      timeout=WEBHOOK_TIMEOUT_S, allow_redirects=False)
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"HTTP {r.status_code}")


def deliver_due(conn: sqlite3.Connection, post: Callable[[str, bytes, dict], None] = _post) -> int:
    """Send every delivery that's due. Background only. Returns how many were tried."""
    from . import events
    now = now_iso()
    due = conn.execute(
        "SELECT d.id, d.event_id, d.attempts, w.id AS webhook_id, w.url, w.secret FROM webhook_deliveries d "
        "JOIN webhooks w ON w.id = d.webhook_id WHERE d.done_at IS NULL AND d.next_at <= ? AND w.enabled = 1 "
        "ORDER BY d.next_at, d.id LIMIT ?", (now, BATCH)).fetchall()
    for d in due:
        ev = events.get(conn, d["event_id"])
        if ev is None:
            with conn:
                conn.execute("UPDATE webhook_deliveries SET done_at = ?, error = 'event expired' WHERE id = ?",
                             (now, d["id"]))
            continue
        body = json.dumps(ev, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Signature": sign(body, d["secret"]),
                   "X-Event-Type": ev["type"], "X-Event-Id": str(ev["id"])}
        try:
            post(d["url"], body, headers)
        except Exception as exc:  # noqa: BLE001 - any failure is a failed attempt
            err = (str(exc) or type(exc).__name__)[:200]
            attempts = d["attempts"] + 1
            with conn:
                if attempts > len(RETRY_AFTER_S):
                    conn.execute("UPDATE webhook_deliveries SET attempts = ?, done_at = ?, error = ? WHERE id = ?",
                                 (attempts, now, err, d["id"]))
                else:
                    conn.execute("UPDATE webhook_deliveries SET attempts = ?, next_at = ?, error = ? WHERE id = ?",
                                 (attempts, now_iso(RETRY_AFTER_S[attempts - 1]), err, d["id"]))
                conn.execute("UPDATE webhooks SET last_error = ?, last_error_at = ? WHERE id = ?",
                             (err, now, d["webhook_id"]))
            continue
        with conn:
            conn.execute("UPDATE webhook_deliveries SET attempts = ?, done_at = ?, error = NULL WHERE id = ?",
                         (d["attempts"] + 1, now, d["id"]))
            conn.execute("UPDATE webhooks SET last_ok_at = ? WHERE id = ?", (now, d["webhook_id"]))
    return len(due)


def prune(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("DELETE FROM webhook_deliveries WHERE done_at IS NOT NULL AND done_at < ?",
                            (now_iso(-KEEP_DONE_DAYS * 86400),)).rowcount
