"""Patreon post <-> Pocket Casts episode matching (spec section 7, "Matching").

1. Exact title match after normalisation.
2. If several Pocket Casts episodes share the title, or none does: accept a
   unique candidate published within 24 hours with duration within 5 seconds.
3. Otherwise leave unmatched. Never guess: a Pocket Casts episode claimed by
   more than one Patreon episode in the same pass is given to none of them.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

DATE_TOLERANCE = timedelta(hours=24)
DURATION_TOLERANCE_SECS = 5.0

_PUNCTUATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ",
})


@dataclass(frozen=True)
class PatreonSide:
    episode_id: int
    title: str
    published_at: str | None
    duration_secs: float | None


@dataclass(frozen=True)
class PocketSide:
    uuid: str
    title: str
    published_at: str | None
    duration_secs: float | None


def normalise_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).translate(_PUNCTUATION)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def close_enough(patreon: PatreonSide, pocket: PocketSide) -> bool:
    a, b = _parse_time(patreon.published_at), _parse_time(pocket.published_at)
    if a is None or b is None or patreon.duration_secs is None or pocket.duration_secs is None:
        return False
    return (abs(a - b) <= DATE_TOLERANCE
            and abs(patreon.duration_secs - pocket.duration_secs) <= DURATION_TOLERANCE_SECS)


def match_episodes(
    patreon: list[PatreonSide], pocket: list[PocketSide], taken: set[str] = frozenset(),
) -> dict[int, tuple[str, str]]:
    """Returns {patreon episode_id: (pocket uuid, 'auto_title' | 'auto_duration')}."""
    available = [p for p in pocket if p.uuid not in taken]
    by_title: dict[str, list[PocketSide]] = defaultdict(list)
    for p in available:
        by_title[normalise_title(p.title)].append(p)

    # Pass 1: titles. Title matches claim their episodes before any fallback runs.
    title_matches: dict[int, tuple[str, str]] = {}
    no_title: list[PatreonSide] = []
    for ep in patreon:
        same_title = by_title.get(normalise_title(ep.title), [])
        if len(same_title) == 1:
            title_matches[ep.episode_id] = (same_title[0].uuid, "auto_title")
        elif same_title:
            close = [p for p in same_title if close_enough(ep, p)]
            if len(close) == 1:
                title_matches[ep.episode_id] = (close[0].uuid, "auto_title")
            # else: ambiguous title and no unique tiebreak, so leave it
        else:
            no_title.append(ep)
    title_matches = _uncontested(title_matches)

    # Pass 2: date + duration fallback over what's left.
    claimed = {uuid for uuid, _ in title_matches.values()}
    remaining = [p for p in available if p.uuid not in claimed]
    fallback: dict[int, tuple[str, str]] = {}
    for ep in no_title:
        close = [p for p in remaining if close_enough(ep, p)]
        if len(close) == 1:
            fallback[ep.episode_id] = (close[0].uuid, "auto_duration")

    return {**title_matches, **_uncontested(fallback)}


def _uncontested(proposals: dict[int, tuple[str, str]]) -> dict[int, tuple[str, str]]:
    claims = Counter(uuid for uuid, _ in proposals.values())
    return {ep_id: match for ep_id, match in proposals.items() if claims[match[0]] == 1}
