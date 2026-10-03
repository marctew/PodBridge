"""Shared HTTP plumbing: sequential requests with a minimum gap and retries on
transient network errors (the LXC has shown intermittent DNS failures).

Exceptions raised from here never include URLs, headers or bodies.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TIMEOUT_SECS = 30
NETWORK_RETRIES = 3


class TransportError(RuntimeError):
    """Network-level failure after retries (DNS, connect, timeout)."""


class HttpClient:
    def __init__(
        self,
        base: str,
        headers: dict[str, str],
        gap_secs: float,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base = base.rstrip("/")
        self.headers = headers
        self.gap_secs = gap_secs
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def _wait(self) -> None:
        if self._last is not None:
            remaining = self.gap_secs - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self.base + path
        merged = {**self.headers, **(headers or {})}
        for attempt in range(NETWORK_RETRIES):
            self._wait()
            try:
                return self.session.request(
                    method, url, params=params, json=json, headers=merged, timeout=TIMEOUT_SECS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                kind = type(exc).__name__
                if attempt == NETWORK_RETRIES - 1:
                    raise TransportError(f"{kind} after {NETWORK_RETRIES} attempts") from None
                log.warning("Network error (%s) talking to %s, retrying", kind, self.base)
                self._sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")


def json_or_none(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
