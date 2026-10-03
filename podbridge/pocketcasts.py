"""Pocket Casts client (the unofficial API the official apps use).

Verified against the apps' source and live probes (docs/pocketcasts-api.md,
docs/phase0-findings.md):
  - auth: POST /user/login_pocket_casts -> access + refresh token (1 h), POST /user/token to refresh
  - subscriptions: POST /user/podcast/list {v: 1}
  - user state (touched episodes only): POST /user/podcast/episodes {uuid}
  - one episode with state: POST /user/episode {uuid, podcast}
  - catalogue (no auth, gzip): GET cache.pocketcasts.com/mobile/podcast/full/{uuid}
  - write: POST /sync/update_episode with all five fields, like the Android app

The private feed URL in the subscription list is a secret; only its hostname is kept.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from .http import HttpClient, TransportError, json_or_none

API_BASE = "https://api.pocketcasts.com"
CACHE_BASES = ("https://cache.pocketcasts.com", "https://podcast-api.pocketcasts.com")
HEADERS = {"User-Agent": "Pocket Casts", "Accept": "application/json"}
REQUEST_GAP_SECS = 0.5
LOGIN_SCOPE = "mobile"
DEFAULT_TOKEN_LIFETIME_SECS = 3600
TOKEN_SAFETY_MARGIN_SECS = 120

STATUS_UNPLAYED = 1
STATUS_IN_PROGRESS = 2
STATUS_PLAYED = 3


class PocketCastsError(RuntimeError):
    pass


class PocketCastsAuthError(PocketCastsError):
    """Email/password rejected."""


class PocketCastsBlocked(PocketCastsError):
    """HTTP 403/429."""


@dataclass(frozen=True)
class Podcast:
    uuid: str
    title: str
    author: str | None
    feed_host: str | None


@dataclass(frozen=True)
class CatalogueEpisode:
    uuid: str
    title: str
    published_at: str | None
    duration_secs: float | None


@dataclass(frozen=True)
class EpisodeState:
    uuid: str
    status: int
    played_up_to: float
    duration_secs: float | None


class PocketCastsClient(Protocol):
    def check_login(self) -> bool: ...
    def list_podcasts(self) -> list[Podcast]: ...
    def list_episodes(self, podcast_uuid: str) -> tuple[list[CatalogueEpisode], bool]: ...
    def episode_states(self, podcast_uuid: str) -> dict[str, EpisodeState]: ...
    def get_episode_state(self, episode_uuid: str, podcast_uuid: str) -> EpisodeState | None: ...
    def update_episode(self, episode_uuid: str, podcast_uuid: str, position: int, duration: int,
                       status: int) -> None: ...


# --- parsing ---

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _list(body: Any, *keys: str) -> list:
    for key in keys:
        body = body.get(key) if isinstance(body, dict) else None
    return body if isinstance(body, list) else []


def parse_podcasts(body: Any) -> list[Podcast]:
    out = []
    for item in _list(body, "podcasts"):
        if not isinstance(item, dict) or not _str(item.get("uuid")):
            continue
        out.append(Podcast(
            uuid=item["uuid"],
            title=_str(item.get("title")) or item["uuid"],
            author=_str(item.get("author")),
            feed_host=urlparse(_str(item.get("url")) or "").hostname,
        ))
    return out


def parse_catalogue(body: Any) -> tuple[list[CatalogueEpisode], bool]:
    out = []
    for item in _list(body, "podcast", "episodes"):
        if not isinstance(item, dict) or not _str(item.get("uuid")):
            continue
        out.append(CatalogueEpisode(
            uuid=item["uuid"],
            title=(_str(item.get("title")) or "").strip() or item["uuid"],
            published_at=_str(item.get("published")),
            duration_secs=_num(item.get("duration")),
        ))
    has_more = isinstance(body, dict) and body.get("has_more_episodes") is True
    return out, has_more


def parse_state(item: Any) -> EpisodeState | None:
    if not isinstance(item, dict) or not _str(item.get("uuid")):
        return None
    status = item.get("playingStatus")
    return EpisodeState(
        uuid=item["uuid"],
        status=status if status in (STATUS_UNPLAYED, STATUS_IN_PROGRESS, STATUS_PLAYED) else STATUS_UNPLAYED,
        played_up_to=_num(item.get("playedUpTo")) or 0.0,
        duration_secs=_num(item.get("duration")),
    )


def parse_states(body: Any) -> dict[str, EpisodeState]:
    states = (parse_state(i) for i in _list(body, "episodes"))
    return {s.uuid: s for s in states if s}


# --- HTTP client ---

class TokenCache:
    """Process-wide access token; the refresh token lives encrypted in settings."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.lock = threading.Lock()
        self._clock = clock
        self.access_token: str | None = None
        self.expires_at = 0.0

    def valid(self) -> str | None:
        if self.access_token and self._clock() < self.expires_at - TOKEN_SAFETY_MARGIN_SECS:
            return self.access_token
        return None

    def set(self, token: str, lifetime_secs: float) -> None:
        self.access_token = token
        self.expires_at = self._clock() + lifetime_secs

    def clear(self) -> None:
        self.access_token = None
        self.expires_at = 0.0


class HttpPocketCastsClient:
    def __init__(
        self,
        email: str,
        password: str,
        refresh_token: str | None,
        on_refresh_token: Callable[[str], None],
        tokens: TokenCache,
        api: HttpClient | None = None,
        caches: list[HttpClient] | None = None,
    ):
        self.email = email
        self.password = password
        self.refresh_token = refresh_token
        self.on_refresh_token = on_refresh_token
        self.tokens = tokens
        self.api = api or HttpClient(API_BASE, dict(HEADERS), gap_secs=REQUEST_GAP_SECS)
        self.caches = caches or [HttpClient(b, dict(HEADERS), gap_secs=REQUEST_GAP_SECS) for b in CACHE_BASES]

    # auth

    def _store_tokens(self, body: Any) -> bool:
        access = _str(body.get("accessToken")) if isinstance(body, dict) else None
        if not access:
            return False
        self.tokens.set(access, _num(body.get("expiresIn")) or DEFAULT_TOKEN_LIFETIME_SECS)
        refresh = _str(body.get("refreshToken"))
        if refresh and refresh != self.refresh_token:
            self.refresh_token = refresh
            self.on_refresh_token(refresh)
        return True

    @staticmethod
    def _raise_for_block(status: int) -> None:
        if status in (403, 429):
            raise PocketCastsBlocked(f"Pocket Casts returned HTTP {status}")

    def _login(self) -> None:
        response = self.api.request("POST", "/user/login_pocket_casts", json={
            "email": self.email, "password": self.password, "scope": LOGIN_SCOPE,
        })
        self._raise_for_block(response.status_code)
        if response.status_code in (400, 401):
            raise PocketCastsAuthError("Pocket Casts rejected the email or password")
        if response.status_code != 200 or not self._store_tokens(json_or_none(response)):
            raise PocketCastsError(f"Pocket Casts login returned HTTP {response.status_code}")

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        response = self.api.request("POST", "/user/token", json={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
        })
        self._raise_for_block(response.status_code)
        return response.status_code == 200 and self._store_tokens(json_or_none(response))

    def _token(self) -> str:
        with self.tokens.lock:
            token = self.tokens.valid()
            if token:
                return token
            if not self._refresh():
                self._login()
            return self.tokens.access_token  # type: ignore[return-value]

    def _post(self, path: str, body: dict) -> Any:
        for attempt in range(2):
            response = self.api.request("POST", path, json=body,
                                        headers={"Authorization": f"Bearer {self._token()}"})
            self._raise_for_block(response.status_code)
            if response.status_code == 401 and attempt == 0:
                self.tokens.clear()
                continue
            if response.status_code != 200:
                raise PocketCastsError(f"{path} returned HTTP {response.status_code}")
            return json_or_none(response)
        raise PocketCastsError(f"{path} rejected a fresh token")

    # API

    def check_login(self) -> bool:
        self._token()
        return True

    def list_podcasts(self) -> list[Podcast]:
        return parse_podcasts(self._post("/user/podcast/list", {"v": 1}))

    def list_episodes(self, podcast_uuid: str) -> tuple[list[CatalogueEpisode], bool]:
        last_error = "no cache host answered"
        for cache in self.caches:
            try:
                response = cache.request("GET", f"/mobile/podcast/full/{podcast_uuid}")
            except TransportError as exc:
                last_error = str(exc)
                continue
            self._raise_for_block(response.status_code)
            if response.status_code == 200:
                episodes, has_more = parse_catalogue(json_or_none(response))
                if episodes:
                    return episodes, has_more
                last_error = "catalogue had no episodes"
            else:
                last_error = f"HTTP {response.status_code}"
        raise PocketCastsError(f"Couldn't load the episode catalogue: {last_error}")

    def episode_states(self, podcast_uuid: str) -> dict[str, EpisodeState]:
        return parse_states(self._post("/user/podcast/episodes", {"uuid": podcast_uuid}))

    def get_episode_state(self, episode_uuid: str, podcast_uuid: str) -> EpisodeState | None:
        return parse_state(self._post("/user/episode", {"uuid": episode_uuid, "podcast": podcast_uuid}))

    def update_episode(self, episode_uuid: str, podcast_uuid: str, position: int, duration: int,
                       status: int) -> None:
        self._post("/sync/update_episode", {
            "uuid": episode_uuid, "podcast": podcast_uuid,
            "position": int(position), "duration": int(duration), "status": int(status),
        })
