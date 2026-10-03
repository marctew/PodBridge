"""Test doubles for the API clients and the HTTP layer."""

from __future__ import annotations

from typing import Any

from podbridge.patreon import Collection, Post


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

    def check_session(self) -> bool:
        if self.error:
            raise self.error
        return self.logged_in

    def list_posts(self, campaign_id: str, collection_id: str) -> list[Post]:
        self.list_calls.append((campaign_id, collection_id))
        return list(self.posts)

    def list_collections(self, campaign_id: str) -> list[Collection]:
        return list(self.collections)
