"""Phase 0 probe for the unofficial Pocket Casts API (spec section 5).

READ-ONLY: never calls /sync/update_episode or anything else that changes state.

Paths come from the official Android app source (Automattic/pocket-casts-android,
modules/services/servers/.../sync/*.kt) plus a community client. Notes:
  - Official apps send many calls as protobuf; this probe checks whether the
    JSON forms still work.
  - Episode user state has no titles; titles come from the cache host.

Reads POCKETCASTS_EMAIL / POCKETCASTS_PASSWORD (env or .env) and answers:
  1. Login: does /user/login_pocket_casts (access + refresh token) work, or only /user/login?
  2. Refresh: does /user/token with a refresh token work?
  3. Subscriptions: does JSON /user/podcast/list work, does it include titles, is the private feed there?
  4. Episode state: /user/podcast/episodes shape and playingStatus distribution.
  5. Metadata: does cache.pocketcasts.com serve the private feed, and do its UUIDs match step 4?

Prints only field paths, types, counts, booleans and episode titles/dates
(needed to judge matching). Never prints tokens, passwords, feed URLs or full UUIDs.

Usage:  python scripts/probe_pocketcasts.py
"""

from __future__ import annotations

import os
from collections import Counter

from _probe_common import Http, dig, heading, load_dotenv, require_env, show_paths

load_dotenv()

API = "https://api.pocketcasts.com"
CACHE = "https://cache.pocketcasts.com"
PC_HEADERS = {"User-Agent": "Pocket Casts", "X-App-Language": "en-GB"}
MATCH = os.environ.get("POCKETCASTS_PODCAST_MATCH", "Button Boys").casefold()
MAX_CACHE_LOOKUPS = 100


def short(uuid: str | None) -> str:
    return f"{uuid[:8]}..." if uuid else "None"


def probe_login(http: Http, email: str, password: str) -> tuple[str | None, str | None]:
    heading("1. Login")
    body = {"email": email, "password": password, "scope": "mobile"}
    status, resp = http.request("POST", "/user/login_pocket_casts", body)
    access, refresh = dig(resp, "accessToken"), dig(resp, "refreshToken")
    print(f"  /user/login_pocket_casts (scope=mobile): HTTP {status}, accessToken={bool(access)}, "
          f"refreshToken={bool(refresh)}, tokenType={dig(resp, 'tokenType')!r}, "
          f"expiresIn={dig(resp, 'expiresIn')!r}")
    if access:
        return access, refresh
    body["scope"] = "webplayer"
    status, resp = http.request("POST", "/user/login", body)
    token = dig(resp, "token")
    print(f"  /user/login (scope=webplayer): HTTP {status}, token={bool(token)}")
    return token, None


def probe_refresh(http: Http, refresh: str | None) -> str | None:
    heading("2. Token refresh")
    if not refresh:
        print("  skipped: no refresh token from login")
        return None
    status, resp = http.request(
        "POST", "/user/token", {"grant_type": "refresh_token", "refresh_token": refresh}
    )
    access = dig(resp, "accessToken")
    print(f"  /user/token: HTTP {status}, accessToken={bool(access)}, "
          f"refreshToken returned={bool(dig(resp, 'refreshToken'))}, expiresIn={dig(resp, 'expiresIn')!r}")
    return access


def probe_subscriptions(http: Http, cache: Http) -> str | None:
    heading("3. Subscriptions: POST /user/podcast/list (JSON)")
    podcasts = []
    for body in ({"v": 1}, {"v": "2", "m": "mobile"}):
        status, resp = http.request("POST", "/user/podcast/list", body)
        podcasts = dig(resp, "podcasts", default=[]) or []
        print(f"  body={body}: HTTP {status}, podcasts={len(podcasts)}")
        if podcasts:
            break
    if not podcasts:
        print("  ! JSON list returned nothing; the protobuf form may be required.")
        return None
    print("  first podcast paths:")
    show_paths(podcasts[0], indent="    ", limit=30)
    with_title = sum(bool(p.get("title")) for p in podcasts)
    print(f"  podcasts with title in list response: {with_title}/{len(podcasts)}")

    for p in podcasts:
        if MATCH in (p.get("title") or "").casefold():
            print(f"  match by list title: uuid={short(p.get('uuid'))} title={p.get('title')!r}")
            return p.get("uuid")

    print(f"  no title match in list; checking cache metadata (up to {MAX_CACHE_LOOKUPS}) ...")
    for p in podcasts[:MAX_CACHE_LOOKUPS]:
        status, resp = cache.request("GET", f"/mobile/podcast/full/{p.get('uuid')}")
        title = dig(resp, "podcast", "title") or ""
        if MATCH in title.casefold():
            print(f"  match via cache: uuid={short(p.get('uuid'))} title={title!r} "
                  f"is_private={dig(resp, 'podcast', 'is_private')!r}")
            return p.get("uuid")
    print(f"  ! No subscription matched {MATCH!r}. Set POCKETCASTS_PODCAST_MATCH and rerun.")
    return None


def probe_episode_state(http: Http, podcast_uuid: str) -> set[str]:
    heading("4. Episode state: POST /user/podcast/episodes")
    status, resp = http.request("POST", "/user/podcast/episodes", {"uuid": podcast_uuid})
    episodes = dig(resp, "episodes", default=[]) or []
    print(f"  HTTP {status}, episodes={len(episodes)}, subscribed={dig(resp, 'subscribed')!r}")
    if episodes:
        print("  first episode paths:")
        show_paths(episodes[0], indent="    ", limit=20)
    statuses = Counter(e.get("playingStatus") for e in episodes)
    print(f"  playingStatus counts: {dict(statuses)}")
    print(f"  episodes with playedUpTo > 0: {sum((e.get('playedUpTo') or 0) > 0 for e in episodes)}")
    print(f"  playedUpTo type: {type(dig(episodes, 0, 'playedUpTo')).__name__}, "
          f"duration type: {type(dig(episodes, 0, 'duration')).__name__}")
    return {e["uuid"] for e in episodes if e.get("uuid")}


def probe_metadata(cache: Http, podcast_uuid: str, state_uuids: set[str]) -> None:
    heading("5. Metadata: GET cache.pocketcasts.com/mobile/podcast/full/{uuid}")
    status, resp = cache.request("GET", f"/mobile/podcast/full/{podcast_uuid}")
    pod = dig(resp, "podcast", default={})
    episodes = pod.get("episodes") or []
    print(f"  HTTP {status}, is_private={pod.get('is_private')!r}, "
          f"episode_count={dig(resp, 'episode_count')!r}, has_more_episodes={dig(resp, 'has_more_episodes')!r}, "
          f"episodes returned={len(episodes)}")
    if not episodes:
        print("  ! No episodes from the cache host. Titles will need another source.")
        return
    print("  first episode paths:")
    show_paths(episodes[0], indent="    ", limit=20)
    cache_uuids = {e.get("uuid") for e in episodes}
    print(f"  state UUIDs found in cache: {len(state_uuids & cache_uuids)}/{len(state_uuids)}")
    print("  newest 5 episodes (for comparing with Patreon titles):")
    for e in episodes[:5]:
        print(f"    {e.get('published')!s:28} dur={e.get('duration')!s:>8}  {e.get('title')!r}")


def main() -> None:
    email = require_env("POCKETCASTS_EMAIL")
    password = require_env("POCKETCASTS_PASSWORD")
    http = Http(API, PC_HEADERS)
    cache = Http(CACHE, {"User-Agent": "Pocket Casts"})

    token, refresh = probe_login(http, email, password)
    if not token:
        print("\nLogin failed. Check credentials and rerun.")
        return
    refreshed = probe_refresh(http, refresh)
    http.headers["Authorization"] = f"Bearer {refreshed or token}"

    podcast_uuid = probe_subscriptions(http, cache)
    if not podcast_uuid:
        return
    state_uuids = probe_episode_state(http, podcast_uuid)
    probe_metadata(cache, podcast_uuid, state_uuids)
    print("\nDone (read-only, nothing was changed). Paste this output back to Claude.")


if __name__ == "__main__":
    main()
