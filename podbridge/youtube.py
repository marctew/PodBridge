"""YouTube client (cookie auth against youtube.com's own pages).

YouTube's official API exposes neither watch history nor playback position, so
this reads the pages the website renders for a signed-in user:
  - /feed/history: recent watches with the red "percent watched" bar
  - /watch?v=ID:   channel, publish date and exact length (fetched once per video)
  - /@handle:      resolves a handle to a channel ID
  - /playlist?list=ID: a channel's uploads, or any playlist added as a source (later pages via
                   the page's own continuation tokens)

Both page shapes are undocumented and change; parsing is defensive and all of
it lives in this module. Cookies come from a cookies.txt export; any cookies
YouTube refreshes are handed back via on_cookies_changed so they persist.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.cookies import create_cookie

from .http import BROWSER_UA, HttpClient
from .redact import register_secret

BASE_URL = "https://www.youtube.com"
SESSION_LOCK = threading.RLock()  # serialises every request made with the signed-in cookies
REQUEST_GAP_SECS = 1.5
COOKIE_DOMAINS = ("youtube.com", "google.com")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Public playlists: PL (user-made), OL (albums/podcasts), UU/UULF (uploads). Not RD (mixes), WL/LL (private).
PLAYLIST_ID = re.compile(r"^(?:PL|OL|UU)[A-Za-z0-9_-]{10,}$")
MAX_PLAYLIST_PAGES = 10  # ~100 videos a page
FALLBACK_CLIENT_VERSION = "2.20261101.00.00"


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


@dataclass(frozen=True)
class Playlist:
    playlist_id: str
    title: str


class YouTubeClient(Protocol):
    def check_session(self) -> bool: ...
    def history(self) -> list[HistoryItem]: ...
    def video_details(self, video_id: str) -> VideoDetails | None: ...
    def resolve_channel(self, handle_or_id: str) -> Channel | None: ...
    def channel_videos(self, channel_id: str) -> list[PlaylistItem]: ...
    def resolve_playlist(self, url_or_id: str) -> Playlist | None: ...
    def playlist_videos(self, playlist_id: str) -> list[PlaylistItem]: ...


def watch_url(video_id: str) -> str:
    return f"{BASE_URL}/watch?v={video_id}"


def is_channel_id(value: str) -> bool:
    return bool(CHANNEL_ID.match(value))


def playlist_id_from(value: str) -> str | None:
    """The playlist ID in a playlist or watch URL (`list=`), or a bare ID; None if it isn't one."""
    value = value.strip()
    if "list=" in value:
        query = urlsplit(value if "://" in value else f"https://{value}").query
        value = next((v for k, v in parse_qsl(query) if k == "list"), "")
    return value if PLAYLIST_ID.match(value) else None


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


def parse_playlist_page(data: Any, playlist_id: str) -> Playlist | None:
    """Title from the playlist page; None when the page has no playlist (private, deleted, bad ID)."""
    for d in _walk(data):
        meta = d.get("playlistMetadataRenderer")
        if isinstance(meta, dict) and _text(meta.get("title")):
            return Playlist(playlist_id, _text(meta["title"]).strip())
    for d in _walk(data):
        micro = d.get("microformatDataRenderer")
        if isinstance(micro, dict) and _text(micro.get("title")):
            return Playlist(playlist_id, _text(micro["title"]).strip())
    return None


def continuation_token(data: Any) -> str | None:
    """The 'load more' token at the end of a playlist page or continuation response."""
    for d in _walk(data):
        renderer = d.get("continuationItemRenderer")
        if isinstance(renderer, dict):
            for c in _walk(renderer):
                command = c.get("continuationCommand")
                if isinstance(command, dict) and isinstance(command.get("token"), str) and command["token"]:
                    return command["token"]
    return None


def client_version(html: str) -> str:
    match = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([0-9.]+)"', html)
    return match.group(1) if match else FALLBACK_CLIENT_VERSION


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
                 sleep: Callable[[float], None] | None = None, public: PublicYouTubeClient | None = None,
                 load_cookies: Callable[[], str | None] | None = None):
        """`load_cookies` returns the latest stored cookies; it's read before every request so a
        rotation saved by another client instance (scheduler thread, another request) is used
        instead of this instance's stale copy."""
        self.public = public or PublicYouTubeClient(gap_secs=gap_secs, sleep=sleep)
        session = session if session is not None else requests.Session()
        self._load_into(session, cookies_text)
        if not len(session.cookies):
            raise YouTubeSessionExpired("No youtube.com cookies found in the pasted cookies")
        self._loaded_text = cookies_text
        self._saved = serialise_cookies(session.cookies)
        self.on_cookies_changed = on_cookies_changed
        self.load_cookies = load_cookies
        extra = {"sleep": sleep} if sleep else {}
        self.http = HttpClient(BASE_URL, {"User-Agent": BROWSER_UA, "Accept-Language": "en-GB"},
                               gap_secs=gap_secs, session=session, **extra)

    @staticmethod
    def _load_into(session: Any, cookies_text: str) -> None:
        session.cookies.clear()
        for c in parse_cookies(cookies_text):
            register_secret(c["value"])
            session.cookies.set_cookie(create_cookie(c["name"], c["value"], domain=c["domain"], path=c["path"],
                                                     secure=c["secure"], expires=c["expires"]))

    def _refresh_from_store(self) -> None:
        if self.load_cookies is None:
            return
        latest = self.load_cookies()
        if latest and latest not in (self._loaded_text, self._saved):
            self._load_into(self.http.session, latest)
            self._loaded_text = latest
            self._saved = serialise_cookies(self.http.session.cookies)

    def _page(self, path: str) -> str:
        # One signed-in request at a time across the whole process: two clients rotating the
        # same Google session in parallel can leave the stored copy holding a dead cookie.
        with SESSION_LOCK:
            self._refresh_from_store()
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

    def resolve_playlist(self, url_or_id: str) -> Playlist | None:
        return self.public.resolve_playlist(url_or_id)

    def playlist_videos(self, playlist_id: str) -> list[PlaylistItem]:
        return self.public.playlist_videos(playlist_id)


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
        return self._checked(self.http.request("GET", path), path)

    def _browse(self, token: str, version: str) -> Any:
        """One continuation of a playlist (the same call the page's own 'load more' makes)."""
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": version, "hl": "en", "gl": "GB"}},
                "continuation": token}
        response = self._checked(self.http.request("POST", "/youtubei/v1/browse?prettyPrint=false", json=body,
                                                   headers={"X-YouTube-Client-Name": "1",
                                                            "X-YouTube-Client-Version": version}),
                                 "/youtubei/v1/browse")
        try:
            return json.loads(response)
        except ValueError:
            raise YouTubeError("YouTube's playlist continuation wasn't JSON") from None

    @staticmethod
    def _checked(response: Any, path: str) -> str:
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

    def video_description(self, video_id: str) -> str | None:
        """The video's description (plain text), for its episode page."""
        if not VIDEO_ID.match(video_id):
            return None
        player = extract_json(self._page(f"/watch?v={video_id}"), "ytInitialPlayerResponse")
        details = player.get("videoDetails") if isinstance(player, dict) else None
        text = details.get("shortDescription") if isinstance(details, dict) else None
        return text if isinstance(text, str) and text.strip() else None

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

    def resolve_playlist(self, url_or_id: str) -> Playlist | None:
        playlist_id = playlist_id_from(url_or_id)
        if playlist_id is None:
            return None
        html = self._page(f"/playlist?list={playlist_id}")
        return parse_playlist_page(extract_json(html, "ytInitialData"), playlist_id)

    def playlist_videos(self, playlist_id: str, max_pages: int = MAX_PLAYLIST_PAGES) -> list[PlaylistItem]:
        """Every video in the playlist, in playlist order, following 'load more' up to max_pages.
        A failed continuation keeps what was read so far."""
        if not PLAYLIST_ID.match(playlist_id):
            return []
        html = self._page(f"/playlist?list={playlist_id}")
        data = extract_json(html, "ytInitialData")
        items = parse_playlist(data)
        seen = {i.video_id for i in items}
        token, version = continuation_token(data), client_version(html)
        for _ in range(max_pages - 1):
            if not token:
                break
            try:
                data = self._browse(token, version)
            except YouTubeBlocked:
                raise
            except YouTubeError:
                break
            for item in parse_playlist(data):
                if item.video_id not in seen:
                    seen.add(item.video_id)
                    items.append(item)
            token = continuation_token(data)
        return items

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
