"""YouTube client (cookie auth against youtube.com's own pages).

YouTube's official API exposes neither watch history nor playback position, so
this reads the pages the website renders for a signed-in user:
  - /feed/history: recent watches with the red "percent watched" bar
  - /watch?v=ID:   channel, publish date and exact length (fetched once per video)
  - /@handle:      resolves a handle to a channel ID

Both page shapes are undocumented and change; parsing is defensive and all of
it lives in this module. Cookies come from a cookies.txt export; any cookies
YouTube refreshes are handed back via on_cookies_changed so they persist.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from requests.cookies import create_cookie

from .http import BROWSER_UA, HttpClient
from .redact import register_secret

BASE_URL = "https://www.youtube.com"
REQUEST_GAP_SECS = 1.5
COOKIE_DOMAINS = ("youtube.com", "google.com")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class YouTubeError(RuntimeError):
    pass


class YouTubeSessionExpired(YouTubeError):
    pass


class YouTubeBlocked(YouTubeError):
    pass


@dataclass(frozen=True)
class HistoryItem:
    video_id: str
    title: str
    percent: float | None


@dataclass(frozen=True)
class PlaylistItem:
    video_id: str
    title: str
    duration_secs: float | None


@dataclass(frozen=True)
class VideoDetails:
    video_id: str
    title: str
    channel_id: str | None
    published_at: str | None
    duration_secs: float | None


@dataclass(frozen=True)
class Channel:
    channel_id: str
    title: str


class YouTubeClient(Protocol):
    def check_session(self) -> bool: ...
    def history(self) -> list[HistoryItem]: ...
    def video_details(self, video_id: str) -> VideoDetails | None: ...
    def resolve_channel(self, handle_or_id: str) -> Channel | None: ...
    def channel_videos(self, channel_id: str) -> list[PlaylistItem]: ...


def watch_url(video_id: str) -> str:
    return f"{BASE_URL}/watch?v={video_id}"


# --- cookies ---

def parse_cookies(text: str) -> list[dict]:
    """Netscape cookies.txt, or a raw 'name=value; name2=value2' header as a fallback."""
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _sub, path, secure, expires, name, value = fields
        if not any(domain.lstrip(".").endswith(d) for d in COOKIE_DOMAINS):
            continue
        cookies.append({"domain": domain, "path": path or "/", "secure": secure.upper() == "TRUE",
                        "expires": int(expires) if expires.isdigit() and int(expires) > 0 else None,
                        "name": name, "value": value})
    if not cookies and "=" in text and "\t" not in text:
        for part in text.replace("Cookie:", "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                cookies.append({"domain": ".youtube.com", "path": "/", "secure": True, "expires": None,
                                "name": name, "value": value})
    return cookies


def serialise_cookies(jar) -> str:
    lines = ["# Netscape HTTP Cookie File (saved by PodBridge)"]
    for c in sorted(jar, key=lambda c: (c.domain, c.name)):
        if not any(c.domain.lstrip(".").endswith(d) for d in COOKIE_DOMAINS):
            continue
        lines.append("\t".join([c.domain, "TRUE" if c.domain.startswith(".") else "FALSE", c.path or "/",
                                "TRUE" if c.secure else "FALSE", str(c.expires or 0), c.name, c.value or ""]))
    return "\n".join(lines) + "\n"


# --- page parsing ---

def extract_json(html: str, name: str) -> Any:
    """Pull `ytInitialData` / `ytInitialPlayerResponse` out of a page."""
    match = re.search(rf"{name}\s*=\s*", html)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return None
    return value


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _text(node: Any) -> str | None:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get("simpleText"), str):
            return node["simpleText"]
        if isinstance(node.get("content"), str):
            return node["content"]
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(r.get("text", "") for r in runs if isinstance(r, dict)) or None
    return None


def _first_number(node: Any, keys: tuple[str, ...]) -> float | None:
    for d in _walk(node):
        for key in keys:
            value = d.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


PERCENT_KEYS = ("percentDurationWatched", "startPercent")


def parse_history(data: Any) -> list[HistoryItem]:
    """History items in page order (most recent first); handles videoRenderer and lockupViewModel."""
    items: list[HistoryItem] = []
    seen: set[str] = set()
    for d in _walk(data):
        if "videoRenderer" in d and isinstance(d["videoRenderer"], dict):
            r = d["videoRenderer"]
            video_id, title = r.get("videoId"), _text(r.get("title"))
            percent = _first_number(r.get("thumbnailOverlays"), PERCENT_KEYS)
        elif "lockupViewModel" in d and isinstance(d["lockupViewModel"], dict):
            r = d["lockupViewModel"]
            if r.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
                continue
            video_id = r.get("contentId")
            title = _text(((r.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title"))
            percent = _first_number(r.get("contentImage"), PERCENT_KEYS)
        else:
            continue
        if not isinstance(video_id, str) or not VIDEO_ID.match(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        items.append(HistoryItem(video_id, (title or video_id).strip(),
                                 max(0.0, min(100.0, percent)) if percent is not None else None))
    return items


_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def parse_clock(text: str | None) -> float | None:
    """'1:03:33' / '53:09' -> seconds."""
    match = _CLOCK.match((text or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_playlist(data: Any) -> list[PlaylistItem]:
    """Videos on a playlist page (playlistVideoRenderer or lockupViewModel layouts), in playlist order."""
    items: list[PlaylistItem] = []
    seen: set[str] = set()
    for d in _walk(data):
        if isinstance(d.get("playlistVideoRenderer"), dict):
            r = d["playlistVideoRenderer"]
            video_id, title = r.get("videoId"), _text(r.get("title"))
            length = r.get("lengthSeconds")
            duration = float(length) if str(length or "").isdigit() else parse_clock(_text(r.get("lengthText")))
        elif isinstance(d.get("lockupViewModel"), dict):
            r = d["lockupViewModel"]
            if r.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
                continue
            video_id = r.get("contentId")
            title = _text(((r.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title"))
            badges = [b.get("text") for b in _walk(r.get("contentImage")) if isinstance(b.get("text"), str)]
            duration = next((s for s in map(parse_clock, badges) if s), None)
        else:
            continue
        if not isinstance(video_id, str) or not VIDEO_ID.match(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        items.append(PlaylistItem(video_id, (title or video_id).strip(), duration))
    return items


def uploads_playlists(channel_id: str) -> list[str]:
    """Long-form uploads (UULF…, no Shorts), then all uploads (UU…) as a fallback."""
    suffix = channel_id[2:]
    return [f"UULF{suffix}", f"UU{suffix}"]


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_watch_page(player: Any, video_id: str) -> VideoDetails | None:
    if not isinstance(player, dict):
        return None
    details = player.get("videoDetails") or {}
    micro = ((player.get("microformat") or {}).get("playerMicroformatRenderer")) or {}
    if details.get("videoId") not in (None, video_id):
        return None
    length = details.get("lengthSeconds") or micro.get("lengthSeconds")
    channel = details.get("channelId") or micro.get("externalChannelId")
    return VideoDetails(
        video_id=video_id,
        title=(details.get("title") or _text(micro.get("title")) or video_id).strip(),
        channel_id=channel if isinstance(channel, str) else None,
        published_at=_iso(micro.get("publishDate") or micro.get("uploadDate")),
        duration_secs=float(length) if str(length or "").isdigit() else None,
    )


def parse_channel_page(data: Any) -> Channel | None:
    for d in _walk(data):
        meta = d.get("channelMetadataRenderer")
        if isinstance(meta, dict) and isinstance(meta.get("externalId"), str):
            return Channel(meta["externalId"], meta.get("title") or meta["externalId"])
    return None


def is_logged_in(html: str) -> bool:
    return '"LOGGED_IN":true' in html


# --- HTTP client ---

class HttpYouTubeClient:
    def __init__(self, cookies_text: str, on_cookies_changed: Callable[[str], None],
                 session: Any = None, gap_secs: float = REQUEST_GAP_SECS,
                 sleep: Callable[[float], None] | None = None, public: PublicYouTubeClient | None = None):
        self.public = public or PublicYouTubeClient(gap_secs=gap_secs, sleep=sleep)
        session = session if session is not None else requests.Session()
        for c in parse_cookies(cookies_text):
            register_secret(c["value"])
            session.cookies.set_cookie(create_cookie(c["name"], c["value"], domain=c["domain"], path=c["path"],
                                                     secure=c["secure"], expires=c["expires"]))
        if not len(session.cookies):
            raise YouTubeSessionExpired("No youtube.com cookies found in the pasted cookies")
        self._saved = serialise_cookies(session.cookies)
        self.on_cookies_changed = on_cookies_changed
        extra = {"sleep": sleep} if sleep else {}
        self.http = HttpClient(BASE_URL, {"User-Agent": BROWSER_UA, "Accept-Language": "en-GB"},
                               gap_secs=gap_secs, session=session, **extra)

    def _page(self, path: str) -> str:
        response = self.http.request("GET", path)
        status = response.status_code
        if status in (403, 429):
            raise YouTubeBlocked(f"YouTube returned HTTP {status}")
        if urlsplit(getattr(response, "url", "") or "").hostname == "consent.youtube.com":
            raise YouTubeSessionExpired("YouTube redirected to its cookie consent page; re-export the cookies")
        if status != 200:
            raise YouTubeError(f"YouTube returned HTTP {status} for {path.split('?')[0]}")
        self._persist_cookies()
        return response.text

    def _persist_cookies(self) -> None:
        current = serialise_cookies(self.http.session.cookies)
        if current != self._saved:
            self._saved = current
            for c in self.http.session.cookies:
                register_secret(c.value)
            self.on_cookies_changed(current)

    def check_session(self) -> bool:
        return is_logged_in(self._page("/feed/history"))

    def history(self) -> list[HistoryItem]:
        html = self._page("/feed/history")
        if not is_logged_in(html):
            raise YouTubeSessionExpired("YouTube cookies are no longer signed in")
        data = extract_json(html, "ytInitialData")
        if data is None:
            raise YouTubeError("Couldn't find the history data on the page")
        return parse_history(data)

    # Public pages go through a separate cookieless client: they don't need the login, and
    # keeping them off the signed-in session means fewer cookie rotations and no clash with
    # the background date backfill.

    def video_details(self, video_id: str) -> VideoDetails | None:
        return self.public.video_details(video_id)

    def channel_videos(self, channel_id: str) -> list[PlaylistItem]:
        return self.public.channel_videos(channel_id)

    def resolve_channel(self, handle_or_id: str) -> Channel | None:
        return self.public.resolve_channel(handle_or_id)


class PublicYouTubeClient:
    """youtube.com pages that need no login: watch pages, playlists, channel pages.

    Sends only YouTube's consent cookie (SOCS), so UK/EU requests aren't redirected to the
    cookie-consent page. Never touches the signed-in session."""

    def __init__(self, session: Any = None, gap_secs: float = REQUEST_GAP_SECS,
                 sleep: Callable[[float], None] | None = None):
        session = session if session is not None else requests.Session()
        session.cookies.set_cookie(create_cookie("SOCS", "CAI", domain=".youtube.com", path="/", secure=True))
        extra = {"sleep": sleep} if sleep else {}
        self.http = HttpClient(BASE_URL, {"User-Agent": BROWSER_UA, "Accept-Language": "en-GB"},
                               gap_secs=gap_secs, session=session, **extra)

    def _page(self, path: str) -> str:
        response = self.http.request("GET", path)
        status = response.status_code
        if status in (403, 429):
            raise YouTubeBlocked(f"YouTube returned HTTP {status}")
        if urlsplit(getattr(response, "url", "") or "").hostname == "consent.youtube.com":
            raise YouTubeError("YouTube redirected a public page to its consent screen")
        if status != 200:
            raise YouTubeError(f"YouTube returned HTTP {status} for {path.split('?')[0]}")
        return response.text

    def video_details(self, video_id: str) -> VideoDetails | None:
        if not VIDEO_ID.match(video_id):
            return None
        html = self._page(f"/watch?v={video_id}")
        return parse_watch_page(extract_json(html, "ytInitialPlayerResponse"), video_id)

    def channel_videos(self, channel_id: str) -> list[PlaylistItem]:
        """The channel's most recent uploads (first page of the uploads playlist, about 100)."""
        if not CHANNEL_ID.match(channel_id):
            return []
        for playlist in uploads_playlists(channel_id):
            try:
                html = self._page(f"/playlist?list={playlist}")
            except YouTubeBlocked:
                raise
            except YouTubeError:
                continue  # e.g. a channel without a long-form playlist
            items = parse_playlist(extract_json(html, "ytInitialData"))
            if items:
                return items
        return []

    def resolve_channel(self, handle_or_id: str) -> Channel | None:
        value = handle_or_id.strip().rstrip("/")
        for prefix in ("https://www.youtube.com/", "https://youtube.com/", "youtube.com/", "www.youtube.com/"):
            if value.startswith(prefix):
                value = value[len(prefix):]
        if value.startswith("channel/"):
            value = value[len("channel/"):]
        if CHANNEL_ID.match(value):
            path = f"/channel/{value}"
        elif re.fullmatch(r"@?[A-Za-z0-9._-]{3,100}", value):
            path = f"/@{value.lstrip('@')}"
        else:
            return None
        return parse_channel_page(extract_json(self._page(path), "ytInitialData"))
