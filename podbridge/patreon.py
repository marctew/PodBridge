"""Patreon internal web API client (session cookie auth).

These JSON:API endpoints are undocumented and may change, so all knowledge of
their shape lives in this module and parsing is defensive. Shapes were
verified in Phase 0 (docs/phase0-findings.md).

Responses carry signed media URLs and playback tokens. The parsers copy out
only the fields PodBridge needs; nothing else leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .http import BROWSER_UA, HttpClient, json_or_none

BASE_URL = "https://www.patreon.com"
REQUEST_GAP_SECS = 1.5
PAGE_SIZE = 50
MAX_PAGES = 40
POST_FIELDS = "title,post_type,published_at,url,post_file"
JSONAPI = {
    "json-api-version": "1.0",
    "json-api-use-default-includes": "false",
    "json-api-use-default-fields": "false",
}


class PatreonError(RuntimeError):
    pass


class PatreonSessionExpired(PatreonError):
    pass


class PatreonBlocked(PatreonError):
    """HTTP 403/429: Patreon or Cloudflare is pushing back. Stop and try later."""


@dataclass(frozen=True)
class Progress:
    position_secs: float | None
    is_watched: bool
    watch_state: str | None
    updated_at: str | None


@dataclass(frozen=True)
class Post:
    post_id: str
    title: str
    published_at: str | None
    url: str | None
    post_type: str | None
    media_id: str | None
    duration_secs: float | None
    progress: Progress


@dataclass(frozen=True)
class Collection:
    collection_id: str
    title: str
    num_posts: int | None


class PatreonClient(Protocol):
    def check_session(self) -> bool: ...
    def list_posts(self, campaign_id: str, collection_id: str) -> list[Post]: ...
    def list_collections(self, campaign_id: str) -> list[Collection]: ...


# --- parsing (pure functions, tested against fixtures) ---

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_progress(raw: Any) -> Progress:
    raw = raw if isinstance(raw, dict) else {}
    return Progress(
        position_secs=_num(raw.get("position_secs")),
        is_watched=raw.get("is_watched") is True,
        watch_state=_str(raw.get("watch_state")),
        updated_at=_str(raw.get("updated_at")),
    )


def _post_page_url(value: Any) -> str | None:
    url = _str(value)
    if url and url.startswith("/posts/"):
        url = BASE_URL + url
    return url if url and url.startswith(BASE_URL + "/") else None


def parse_post(item: Any) -> Post | None:
    if not isinstance(item, dict) or item.get("type") != "post" or not item.get("id"):
        return None
    attrs = item.get("attributes") or {}
    post_file = attrs.get("post_file") if isinstance(attrs.get("post_file"), dict) else {}
    media_id = post_file.get("media_id")
    return Post(
        post_id=str(item["id"]),
        title=(_str(attrs.get("title")) or "").strip() or f"Post {item['id']}",
        published_at=_str(attrs.get("published_at")),
        url=_post_page_url(attrs.get("url")),
        post_type=_str(attrs.get("post_type")),
        media_id=str(media_id) if media_id not in (None, "") else None,
        duration_secs=_num(post_file.get("duration")),
        progress=parse_progress(post_file.get("progress")),
    )


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _data_list(body: Any) -> list:
    data = _dig(body, "data")
    return data if isinstance(data, list) else []


def parse_posts_page(body: Any) -> tuple[list[Post], str | None]:
    posts = [p for p in (parse_post(i) for i in _data_list(body)) if p]
    return posts, _str(_dig(body, "meta", "pagination", "cursors", "next"))


def parse_collections(body: Any) -> list[Collection]:
    out = []
    for item in _data_list(body):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        attrs = item.get("attributes") or {}
        num = attrs.get("num_posts")
        out.append(Collection(
            collection_id=str(item["id"]),
            title=_str(attrs.get("title")) or f"Collection {item['id']}",
            num_posts=num if isinstance(num, int) else None,
        ))
    return out


# --- HTTP client ---

class HttpPatreonClient:
    def __init__(self, session_id: str, http: HttpClient | None = None):
        self.http = http or HttpClient(
            BASE_URL,
            {"User-Agent": BROWSER_UA, "Accept": "application/json", "Cookie": f"session_id={session_id}"},
            gap_secs=REQUEST_GAP_SECS,
        )

    def _get(self, path: str, params: dict[str, str]) -> tuple[int, Any]:
        response = self.http.request("GET", path, params=params)
        status = response.status_code
        if status in (403, 429):
            raise PatreonBlocked(f"Patreon returned HTTP {status}")
        return status, json_or_none(response)

    def check_session(self) -> bool:
        status, body = self._get("/api/current_user", dict(JSONAPI))
        if status == 401:
            return False
        if status != 200:
            raise PatreonError(f"Session check returned HTTP {status}")
        data = _dig(body, "data")
        return isinstance(data, dict) and bool(data.get("id")) and data.get("type") == "user"

    def list_posts(self, campaign_id: str, collection_id: str) -> list[Post]:
        params = {
            "fields[post]": POST_FIELDS,
            "filter[campaign_id]": campaign_id,
            "filter[collection_id]": collection_id,
            "filter[is_suspended]": "false",
            "filter[include_drops]": "true",
            "filter[is_published]": "true",
            "sort": "collection_order",
            "page[size]": str(PAGE_SIZE),
            **JSONAPI,
        }
        posts: list[Post] = []
        for _ in range(MAX_PAGES):
            status, body = self._get("/api/posts", params)
            if status == 401:
                raise PatreonSessionExpired("Patreon session rejected while listing posts")
            if status != 200:
                raise PatreonError(f"Post list returned HTTP {status}")
            page, cursor = parse_posts_page(body)
            posts.extend(page)
            if not cursor or not page:
                break
            params["page[cursor]"] = cursor
        return posts

    def list_collections(self, campaign_id: str) -> list[Collection]:
        params = {"filter[campaign_id]": campaign_id, "fields[collection]": "title,num_posts", **JSONAPI}
        status, body = self._get("/api/collection", params)
        if status == 401:
            raise PatreonSessionExpired("Patreon session rejected while listing collections")
        if status != 200:
            raise PatreonError(f"Collection list returned HTTP {status}")
        return parse_collections(body)
