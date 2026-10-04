"""Test doubles for the API clients and the HTTP layer."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from podbridge.patreon import Collection, Post
from podbridge.pocketcasts import CatalogueEpisode, EpisodeState, Podcast


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    """Stands in for requests.Session; returns queued responses and records calls."""

    def __init__(self, *responses: FakeResponse | Exception):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "json": json, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePatreonClient:
    def __init__(self, posts: list[Post] | None = None, logged_in: bool = True,
                 collections: list[Collection] | None = None, error: Exception | None = None):
        self.posts = posts or []
        self.logged_in = logged_in
        self.collections = collections or []
        self.error = error
        self.list_calls: list[tuple[str, str]] = []
        self.post_texts: dict[str, str] = {}

    def check_session(self) -> bool:
        if self.error:
            raise self.error
        return self.logged_in

    def list_posts(self, campaign_id: str, collection_id: str) -> list[Post]:
        self.list_calls.append((campaign_id, collection_id))
        return list(self.posts)

    def list_collections(self, campaign_id: str) -> list[Collection]:
        return list(self.collections)

    def get_post(self, post_id: str) -> Post | None:
        self.list_calls.append(("post", post_id))
        return next((p for p in self.posts if p.post_id == post_id), None)

    def get_post_text(self, post_id: str) -> str | None:
        self.list_calls.append(("text", post_id))
        if self.error:
            raise self.error
        return self.post_texts.get(post_id)


class FakePocketCastsClient:
    def __init__(self, podcasts: list[Podcast] | None = None,
                 catalogue: list[CatalogueEpisode] | None = None,
                 states: dict[str, EpisodeState] | None = None,
                 error: Exception | None = None, truncated: bool = False):
        self.podcasts = podcasts or []
        self.catalogue = catalogue or []
        self.states = states or {}
        self.error = error
        self.truncated = truncated
        self.updates: list[tuple] = []
        self.stars: list[tuple] = []
        self.star_error: Exception | None = None
        self.notes: dict[str, str] = {}

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def check_login(self) -> bool:
        self._maybe_fail()
        return True

    def list_podcasts(self) -> list[Podcast]:
        self._maybe_fail()
        return list(self.podcasts)

    def list_episodes(self, podcast_uuid: str):
        self._maybe_fail()
        return list(self.catalogue), self.truncated

    def episode_states(self, podcast_uuid: str) -> dict[str, EpisodeState]:
        self._maybe_fail()
        return dict(self.states)

    def get_episode_state(self, episode_uuid: str, podcast_uuid: str) -> EpisodeState | None:
        self._maybe_fail()
        return self.states.get(episode_uuid)

    def update_episode(self, episode_uuid, podcast_uuid, position, duration, status) -> None:
        self._maybe_fail()
        self.updates.append((episode_uuid, podcast_uuid, position, duration, status))

    def set_starred(self, episode_uuid, podcast_uuid, starred) -> None:
        self._maybe_fail()
        if self.star_error:
            raise self.star_error
        self.stars.append((episode_uuid, podcast_uuid, starred))
        if episode_uuid in self.states:
            self.states[episode_uuid] = replace(self.states[episode_uuid], starred=bool(starred))

    def show_notes(self, podcast_uuid, episode_uuid) -> str | None:
        self._maybe_fail()
        return self.notes.get(episode_uuid)
