"""Backup and restore of the SQLite database (sources, matches, progress, history, settings).

Credentials stay Fernet-encrypted inside the backup, so a restore needs the same
ENCRYPTION_KEY, or can drop the credentials and you re-enter them. The artwork cache isn't
included: it's re-downloaded as needed.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .crypto import SecretBox, SecretError
from .db import connect, migrate, migrations

REQUIRED_TABLES = {"sources", "episodes", "progress", "settings"}
SQLITE_HEADER = b"SQLite format 3\x00"


class BackupError(ValueError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    schema_version: int
    episodes: int
    sources: int
    has_secrets: bool
    secrets_readable: bool


def make_backup(live: sqlite3.Connection) -> bytes:
    """A consistent copy of the live database (SQLite's online backup, safe during a sync)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "backup.db"
        dest = sqlite3.connect(path)
        try:
            live.backup(dest)
        finally:
            dest.close()
        return path.read_bytes()


def inspect(path: Path, box: SecretBox) -> BackupInfo:
    if path.read_bytes()[:16] != SQLITE_HEADER:
        raise BackupError("That isn't a PodBridge backup (not a SQLite database).")
    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The backup file is damaged (integrity check failed).")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not REQUIRED_TABLES <= tables:
            raise BackupError("That isn't a PodBridge backup (tables are missing).")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > migrations()[-1][0]:
            raise BackupError("That backup is from a newer PodBridge. Update PodBridge first.")
        secrets = [r[0] for r in conn.execute("SELECT value FROM settings WHERE is_secret = 1")]
        readable = True
        for value in secrets:
            try:
                box.decrypt(value)
            except SecretError:
                readable = False
                break
        return BackupInfo(
            schema_version=version,
            episodes=conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            sources=conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            has_secrets=bool(secrets), secrets_readable=readable,
        )
    finally:
        conn.close()


def restore(live: sqlite3.Connection, path: Path, box: SecretBox, drop_secrets: bool = False) -> BackupInfo:
    """Replace the live database's contents with the backup's (brought up to the current schema).
    Refuses if the backup's credentials can't be decrypted, unless drop_secrets."""
    info = inspect(path, box)
    if info.has_secrets and not info.secrets_readable and not drop_secrets:
        raise BackupError("This backup's credentials were encrypted with a different ENCRYPTION_KEY. "
                          "Restore without credentials and re-enter them, or use the original key.")
    src = connect(path)
    try:
        migrate(src)
        if drop_secrets:
            with src:
                src.execute("DELETE FROM settings WHERE is_secret = 1")
                src.execute("DELETE FROM settings WHERE key LIKE '%\\_status' ESCAPE '\\' "
                            "OR key LIKE '%\\_verified\\_at' ESCAPE '\\'")
        src.backup(live)
    finally:
        src.close()
    return info
