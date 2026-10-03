"""SQLite access and numbered SQL migrations (tracked with PRAGMA user_version)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app, g

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrations() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        found.append((int(path.name.split("_", 1)[0]), path))
    return found


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order, each in its own transaction. Returns the schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, path in migrations():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        # SQLite's table-rebuild procedure: foreign keys off (outside the transaction) so
        # DROP TABLE doesn't cascade, then verify integrity before committing.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};")
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise sqlite3.IntegrityError(f"Migration {path.name} broke {len(problems)} foreign key(s)")
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        log.info("Applied migration %s", path.name)
        current = version
    return current


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["PODBRIDGE"].database_path)
    return g.db


def _close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app: Flask) -> None:
    conn = connect(app.config["PODBRIDGE"].database_path)
    try:
        migrate(conn)
    finally:
        conn.close()
    app.teardown_appcontext(_close_db)
