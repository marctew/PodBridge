"""Artwork cache: series art and episode thumbnails.

Images are downloaded once, stored under <data dir>/artwork and served by /art/<key>
(behind the login). Source URLs are never stored or logged: Patreon's are signed and
expire, so Patreon thumbnails are downloaded during discovery while the URL is fresh.

Keys:
  yt:<video id>        YouTube thumbnail (public, fetched on first view)
  podcast:<uuid>       Pocket Casts series art (public CDN, fetched on first view)
  patreon:<post id>    Patreon post thumbnail (fetched during discovery only)
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .db import utcnow
from .http import BROWSER_UA

log = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^(yt|podcast|patreon):[A-Za-z0-9_-]{1,64}$")
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECS = 15
RETRY_FAILED_AFTER = timedelta(days=1)
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_PATREON_DOWNLOADS_PER_RUN = 30


def public_urls(key: str) -> list[str]:
    """Where to fetch artwork that doesn't need a signed URL. Patreon has none."""
    kind, _, ident = key.partition(":")
    if kind == "yt":
        return [f"https://i.ytimg.com/vi/{ident}/mqdefault.jpg"]  # 320x180, 16:9
    if kind == "podcast":
        return [f"https://static.pocketcasts.com/discover/images/webp/480/{ident}.webp",
                f"https://static.pocketcasts.com/discover/images/400/{ident}.jpg",
                f"https://static.pocketcasts.com/discover/images/960/{ident}.jpg"]
    return []


def _filename(key: str, content_type: str) -> str:
    return f"{hashlib.sha1(key.encode()).hexdigest()}.{EXTENSIONS[content_type]}"


def download(url: str, session=None) -> tuple[bytes, str] | None:
    """Fetch an image; None unless it's a JPEG/PNG/WebP under MAX_BYTES. Never logs the URL."""
    getter = session or requests
    try:
        with getter.get(url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT_SECS, stream=True) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if response.status_code != 200 or content_type not in EXTENSIONS:
                return None
            data = b""
            for chunk in response.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
            return data, content_type
    except (requests.RequestException, OSError) as exc:
        log.info("Artwork download failed (%s)", type(exc).__name__)
        return None


def cached(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artwork WHERE key = ?", (key,)).fetchone()


def has_art(conn: sqlite3.Connection, key: str) -> bool:
    row = cached(conn, key)
    return row is not None and bool(row["ok"])


def ensure(conn: sqlite3.Connection, art_dir: Path, key: str, urls: list[str] | None = None,
           session=None) -> Path | None:
    """Path to the cached image for `key`, downloading it from `urls` (or the public URLs) if
    needed. A failed attempt is remembered and retried after a day."""
    row = cached(conn, key)
    if row is not None and row["ok"]:
        path = art_dir / row["filename"]
        if path.is_file():
            return path
    elif row is not None:
        fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - fetched < RETRY_FAILED_AFTER:
            return None
    for url in urls if urls is not None else public_urls(key):
        result = download(url, session)
        if result is None:
            continue
        data, content_type = result
        art_dir.mkdir(parents=True, exist_ok=True)
        filename = _filename(key, content_type)
        (art_dir / filename).write_bytes(data)
        with conn:
            conn.execute("INSERT OR REPLACE INTO artwork (key, filename, content_type, ok, fetched_at) "
                         "VALUES (?, ?, ?, 1, ?)", (key, filename, content_type, utcnow()))
        return art_dir / filename
    with conn:
        # Several thumbnails can ask for the same series art at once: a failure must never
        # overwrite another request's success.
        conn.execute("INSERT INTO artwork (key, filename, content_type, ok, fetched_at) VALUES (?, NULL, NULL, 0, ?) "
                     "ON CONFLICT (key) DO UPDATE SET fetched_at = excluded.fetched_at WHERE artwork.ok = 0",
                     (key, utcnow()))
    row = cached(conn, key)
    return art_dir / row["filename"] if row is not None and row["ok"] and row["filename"] else None
