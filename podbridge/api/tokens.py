"""API tokens (API standard section 3): made in Settings, shown once, stored as sha256."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

from . import now_iso

PREFIX = "pb_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn: sqlite3.Connection, name: str, scope: str) -> str:
    """Make a token and return it; only its hash is kept."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the token a name, e.g. Home Assistant.")
    if scope not in ("read", "write"):
        raise ValueError("Access must be read only, or read and write.")
    token = PREFIX + secrets.token_urlsafe(32)
    with conn:
        conn.execute("INSERT INTO api_tokens (name, token_hash, scope, created_at) VALUES (?, ?, ?, ?)",
                     (name, _hash(token), scope, now_iso()))
    return token


def check(conn: sqlite3.Connection, token: str) -> dict | None:
    """{id, name, scope} for a known token (and note its use), else None."""
    row = conn.execute("SELECT id, name, scope FROM api_tokens WHERE token_hash = ?",
                       (_hash(token.strip()),)).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return dict(row)


def list_tokens(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, scope, created_at, last_used_at FROM api_tokens ORDER BY created_at DESC, id DESC")]


def revoke(conn: sqlite3.Connection, token_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,)).rowcount > 0
