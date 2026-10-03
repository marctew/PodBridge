"""Shared helpers for the Phase 0 probe scripts.

The probes must never print secrets, tokens or URLs. Output is limited to
field paths, types, booleans, counts and a small whitelist of enum-like values.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Keys whose values are safe to print (short enums / labels, never URLs or tokens).
SAFE_VALUE_KEYS = {
    "type",
    "watch_state",
    "is_watched",
    "media_type",
    "post_type",
    "owner_type",
    "episode_type",
    "current_user_can_view",
    "playingStatus",
}

REQUEST_GAP_SECS = 1.5


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Existing environment variables win."""
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing {name}. Set it in .env or the environment.")
    return value


class Http:
    """Sequential, rate-limited JSON client. Never prints request details."""

    def __init__(self, base: str, headers: dict[str, str]):
        self.base = base.rstrip("/")
        self.headers = {"User-Agent": BROWSER_UA, "Accept": "application/json", **headers}
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < REQUEST_GAP_SECS:
            time.sleep(REQUEST_GAP_SECS - gap)
        self._last = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
        omit_headers: tuple[str, ...] = (),
    ) -> tuple[int, Any]:
        self._wait()
        headers = {k: v for k, v in self.headers.items() if k not in omit_headers}
        headers.update(extra_headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        headers.setdefault("Accept-Encoding", "gzip")
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            status, raw, resp_headers = fetch(req)
        except OSError as exc:
            return 0, {"_error": describe_error(exc)}
        if status in (403, 429):
            print(f"  ! HTTP {status}: backing off, stopping this probe run to be gentle.")
            sys.exit(2)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, {"_non_json": describe_body(raw, resp_headers)}


def fetch(req: urllib.request.Request) -> tuple[int, bytes, Any]:
    """Perform a request; returns (status, decompressed body, headers)."""
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        status, raw, headers = exc.code, exc.read(), exc.headers
    if raw[:2] == b"\x1f\x8b" or (headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return status, raw, headers


def describe_error(exc: OSError) -> str:
    reason = getattr(exc, "reason", exc)
    return type(reason).__name__


def describe_body(raw: bytes, headers: Any) -> str:
    """Classify a non-JSON body without echoing its contents."""
    head = raw[:300].lstrip().lower()
    if not raw:
        kind = "empty"
    elif head.startswith(b"<?xml") or head.startswith(b"<rss"):
        kind = "xml"
    elif head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        kind = "html (challenge page?)" if b"cloudflare" in raw[:5000].lower() or b"challenge" in raw[:5000].lower() else "html"
    elif raw[:2] == b"\x1f\x8b":
        kind = "gzip (undecodable)"
    else:
        kind = "other"
    return (f"kind={kind}, bytes={len(raw)}, content-type={headers.get('Content-Type')!r}, "
            f"content-encoding={headers.get('Content-Encoding')!r}")


def field_paths(obj: Any, prefix: str = "") -> list[str]:
    """Flatten JSON into 'a.b[].c: type' lines; values shown only for safe keys."""
    out: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, (dict, list)):
                out.extend(field_paths(val, path))
            elif key in SAFE_VALUE_KEYS and (isinstance(val, bool) or isinstance(val, (str, int))):
                out.append(f"{path} = {val!r}")
            else:
                out.append(f"{path}: {type(val).__name__}")
    elif isinstance(obj, list):
        if not obj:
            out.append(f"{prefix}[]: empty")
        for item in obj[:1]:
            out.extend(field_paths(item, f"{prefix}[]"))
    return out


def dig(obj: Any, *keys: Any, default: Any = None) -> Any:
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return default
        if obj is None:
            return default
    return obj


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


def show_paths(obj: Any, indent: str = "  ", limit: int = 80) -> None:
    lines = field_paths(obj)
    for line in lines[:limit]:
        print(indent + line)
    if len(lines) > limit:
        print(f"{indent}... {len(lines) - limit} more paths")
