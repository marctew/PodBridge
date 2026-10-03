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
from difflib import SequenceMatcher

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


LOOSE_DATE_TOLERANCE = timedelta(hours=30)
MIN_TITLE_SIMILARITY = 0.45
MIN_SIMILARITY_MARGIN = 0.15


_STOPWORDS = frozenset("a an and are as at be by for from in is it of on or the this to with".split())


def _words(title: str) -> set[str]:
    text = re.sub(r"'s\b", "", normalise_title(title))
    return {w for w in re.findall(r"[a-z0-9]+", text) if w not in _STOPWORDS}


def title_similarity(a: str, b: str) -> float:
    """Best of character similarity and word overlap (Dice), so reordered titles still score."""
    chars = SequenceMatcher(None, normalise_title(a), normalise_title(b)).ratio()
    wa, wb = _words(a), _words(b)
    words = 2 * len(wa & wb) / (len(wa) + len(wb)) if wa and wb else 0.0
    return max(chars, words)


def match_episodes_loose(
    source: list[PatreonSide], pocket: list[PocketSide], taken: set[str] = frozenset(),
) -> dict[int, tuple[str, str]]:
    """For YouTube sources, where video titles and lengths differ from the podcast's.

    1. Exact normalised title (unique), as for Patreon.
    2. Otherwise Pocket Casts episodes published within 30 hours: a single one wins;
       several are separated by title similarity (best >= 0.45 and 0.15 clear of the next).
    Contested Pocket Casts episodes go to nobody.
    """
    available = [p for p in pocket if p.uuid not in taken]
    by_title: dict[str, list[PocketSide]] = defaultdict(list)
    for p in available:
        by_title[normalise_title(p.title)].append(p)

    title_matches: dict[int, tuple[str, str]] = {}
    rest: list[PatreonSide] = []
    for ep in source:
        same = by_title.get(normalise_title(ep.title), [])
        if len(same) == 1:
            title_matches[ep.episode_id] = (same[0].uuid, "auto_title")
        else:
            rest.append(ep)
    title_matches = _uncontested(title_matches)

    claimed = {uuid for uuid, _ in title_matches.values()}
    remaining = [p for p in available if p.uuid not in claimed]
    date_matches: dict[int, tuple[str, str]] = {}
    for ep in rest:
        when = _parse_time(ep.published_at)
        if when is None:
            continue
        near = [p for p in remaining
                if (t := _parse_time(p.published_at)) is not None and abs(t - when) <= LOOSE_DATE_TOLERANCE]
        if len(near) == 1:
            date_matches[ep.episode_id] = (near[0].uuid, "auto_date")
        elif near:
            scored = sorted(((title_similarity(ep.title, p.title), p) for p in near),
                            key=lambda sp: sp[0], reverse=True)
            best, runner_up = scored[0][0], scored[1][0]
            if best >= MIN_TITLE_SIMILARITY and best - runner_up >= MIN_SIMILARITY_MARGIN:
                date_matches[ep.episode_id] = (scored[0][1].uuid, "auto_date")

    return {**title_matches, **_uncontested(date_matches)}


def _uncontested(proposals: dict[int, tuple[str, str]]) -> dict[int, tuple[str, str]]:
    claims = Counter(uuid for uuid, _ in proposals.values())
    return {ep_id: match for ep_id, match in proposals.items() if claims[match[0]] == 1}
