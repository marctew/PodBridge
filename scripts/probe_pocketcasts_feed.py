"""Phase 0b: find where Pocket Casts exposes the private feed's full episode list.

The first probe showed /user/podcast/episodes only returns episodes with user
state, and cache.pocketcasts.com returned no episodes for the private feed.
This tries the other candidate sources and reports which ones list episodes
with UUIDs and titles.

READ-ONLY. Prints status codes, top-level keys, counts, hostnames and a few
episode titles. Never prints tokens, feed URLs or full UUIDs.

Usage:  python scripts/probe_pocketcasts_feed.py
"""

from __future__ import annotations

import re
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from _probe_common import (
    BROWSER_UA, Http, describe_body, describe_error, dig, fetch, heading, load_dotenv, require_env,
)
from probe_pocketcasts import API, CACHE, MATCH, PC_HEADERS, probe_login, short

load_dotenv()


def find_episode_lists(obj: Any, path: str = "") -> list[tuple[str, list]]:
    """Every list under a key named 'episodes', anywhere in the response."""
    found: list[tuple[str, list]] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            sub = f"{path}.{key}" if path else key
            if key == "episodes" and isinstance(val, list):
                found.append((sub, val))
            found.extend(find_episode_lists(val, sub))
    return found


def report(label: str, status: int, body: Any, state_uuids: set[str]) -> None:
    keys = sorted(body) if isinstance(body, dict) else type(body).__name__
    print(f"  [{label}] HTTP {status}, top-level keys: {keys}")
    for diag in ("_non_json", "_error"):
        if isinstance(body, dict) and diag in body:
            print(f"    {diag}: {body[diag]}")
    for path, eps in find_episode_lists(body):
        uuids = {e.get("uuid") for e in eps if isinstance(e, dict)}
        titled = sum(bool(isinstance(e, dict) and e.get("title")) for e in eps)
        print(f"    {path}: {len(eps)} episodes, with title={titled}, "
              f"contains known state episode={bool(uuids & state_uuids)}")
        for e in eps[:3]:
            if isinstance(e, dict) and e.get("title"):
                print(f"      {str(e.get('published'))[:25]:25} dur={e.get('duration')!s:>8}  {e.get('title')!r}")


def probe_rss(feed_url: str) -> None:
    heading("RSS feed (subscription url field)")
    time.sleep(1.5)
    req = urllib.request.Request(
        feed_url, headers={"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
    )
    try:
        status, raw, headers = fetch(req)
    except OSError as exc:  # report the type only, never the URL
        print(f"  fetch failed: {describe_error(exc)}")
        return
    text = raw.decode("utf-8", "replace")
    items = re.findall(r"<item\b.*?</item>", text, re.S)
    has_guid = sum("<guid" in i for i in items)
    print(f"  HTTP {status}, items={len(items)}, items with guid={has_guid}")
    print(f"    body: {describe_body(raw, headers)}")
    for item in items[:3]:
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        print(f"    {title.group(1).strip() if title else '?'!r}")


def main() -> None:
    email = require_env("POCKETCASTS_EMAIL")
    password = require_env("POCKETCASTS_PASSWORD")
    http = Http(API, PC_HEADERS)
    token, _ = probe_login(http, email, password)
    if not token:
        return
    http.headers["Authorization"] = f"Bearer {token}"

    heading("Subscriptions matching the title")
    _, resp = http.request("POST", "/user/podcast/list", {"v": 1})
    matches = [p for p in dig(resp, "podcasts", default=[]) if MATCH in (p.get("title") or "").casefold()]
    for p in matches:
        print(f"  uuid={short(p.get('uuid'))} title={p.get('title')!r} author={p.get('author')!r} "
              f"feed host={urlparse(p.get('url') or '').hostname!r}")
    if not matches:
        print("  none")
        return

    cache = Http(CACHE, {"User-Agent": "Pocket Casts"})
    podapi = Http("https://podcast-api.pocketcasts.com", {"User-Agent": "Pocket Casts"})
    for p in matches:
        uuid = p["uuid"]
        heading(f"Candidates for {short(uuid)} ({p.get('title')!r})")
        status, body = http.request("POST", "/user/podcast/episodes", {"uuid": uuid})
        state = dig(body, "episodes", default=[]) or []
        state_uuids = {e.get("uuid") for e in state}
        report("api /user/podcast/episodes", status, body, set())

        if state_uuids:
            ep_uuid = next(iter(state_uuids))
            status, body = http.request("POST", "/user/episode", {"uuid": ep_uuid, "podcast": uuid})
            print(f"  [api /user/episode (one known episode)] HTTP {status}, "
                  f"keys: {sorted(body) if isinstance(body, dict) else type(body).__name__}, "
                  f"has title={bool(dig(body, 'title'))}")

        if p.get("url"):
            probe_rss(p["url"])

        # Unauthenticated metadata hosts last: a 403/429 here stops the run.
        for label, client, path in (
            ("cache /mobile/podcast/full/{uuid}", cache, f"/mobile/podcast/full/{uuid}"),
            ("cache /mobile/podcast/full/{uuid}/0/3/1000", cache, f"/mobile/podcast/full/{uuid}/0/3/1000"),
            ("cache /mobile/podcast/findbyuuid/{uuid}", cache, f"/mobile/podcast/findbyuuid/{uuid}"),
            ("podcast-api /podcast/full/{uuid}", podapi, f"/podcast/full/{uuid}"),
            ("podcast-api /mobile/podcast/full/{uuid}", podapi, f"/mobile/podcast/full/{uuid}"),
        ):
            status, body = client.request("GET", path)
            report(label, status, body, state_uuids)

    print("\nDone (read-only). Paste this output back to Claude.")


if __name__ == "__main__":
    main()
