"""Key/value settings in SQLite. Secret values are Fernet-encrypted and never leave
this module except through get_secret(), which also registers them for log redaction."""

from __future__ import annotations

import sqlite3

from flask import current_app

from .crypto import SecretBox
from .db import get_db, utcnow
from .redact import register_secret

SECRET_KEYS = frozenset({
    "patreon_session_id",
    "pocketcasts_email",
    "pocketcasts_password",
    "pocketcasts_refresh_token",
    "alert_webhook_url",
})

DEFAULTS = {
    "sync_interval_minutes": "15",
    "dry_run": "1",
}

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440


class SettingsStore:
    def __init__(self, conn: sqlite3.Connection, box: SecretBox):
        self._conn = conn
        self._box = box

    def _row(self, key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT value, is_secret, updated_at FROM settings WHERE key = ?", (key,)
        ).fetchone()

    def _upsert(self, key: str, value: str, is_secret: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value, is_secret, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "is_secret = excluded.is_secret, updated_at = excluded.updated_at",
                (key, value, int(is_secret), utcnow()),
            )

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in SECRET_KEYS:
            raise KeyError(f"{key} is secret; use get_secret()")
        row = self._row(key)
        if row is not None:
            return row["value"]
        return DEFAULTS.get(key, default)

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def get_int(self, key: str) -> int:
        return int(self.get(key) or 0)

    def set(self, key: str, value: str) -> None:
        if key in SECRET_KEYS:
            raise KeyError(f"{key} is secret; use set_secret()")
        self._upsert(key, value, is_secret=False)

    def get_secret(self, key: str) -> str | None:
        if key not in SECRET_KEYS:
            raise KeyError(f"{key} is not a secret setting")
        row = self._row(key)
        if row is None:
            return None
        value = self._box.decrypt(row["value"])
        register_secret(value)
        return value

    def set_secret(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(f"{key} is not a secret setting")
        register_secret(value)
        self._upsert(key, self._box.encrypt(value), is_secret=True)

    def delete(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def is_set(self, key: str) -> bool:
        return self._row(key) is not None

    def updated_at(self, key: str) -> str | None:
        row = self._row(key)
        return row["updated_at"] if row else None


def get_store() -> SettingsStore:
    return SettingsStore(get_db(), current_app.extensions["secret_box"])
