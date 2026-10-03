"""Interpretation of Patreon progress for syncing.

Patreon sets `is_watched` well before the end (observed: flagged at 40:44 of
47:18, about 86%), so the flag alone does not mean "finished". Decision
(2026-10-03, spec section 7 amendment): sync the real position, and treat an
episode as played only when the position is within PLAYED_TAIL_SECS of the
end, or when Patreon says watched but gives no usable position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PLAYED_TAIL_SECS = 60.0
AHEAD_THRESHOLD_SECS = 15.0  # never rewind; only write when Patreon is this far ahead
PC_PLAYED = 3
PC_IN_PROGRESS = 2
PC_UNPLAYED = 1


@dataclass(frozen=True)
class PatreonPlayback:
    played: bool
    position_secs: float | None  # None when nothing to sync (not started / no data)


def interpret_patreon(position_secs: float | None, is_watched: bool,
                      duration_secs: float | None) -> PatreonPlayback:
    near_end = (position_secs is not None and duration_secs is not None
                and position_secs >= duration_secs - PLAYED_TAIL_SECS)
    if near_end or (is_watched and (position_secs is None or duration_secs is None)):
        return PatreonPlayback(played=True, position_secs=position_secs)
    if position_secs is not None and position_secs > 0:
        return PatreonPlayback(played=False, position_secs=position_secs)
    return PatreonPlayback(played=False, position_secs=None)


@dataclass(frozen=True)
class Decision:
    action: str  # set_position | mark_played | skipped_behind | skipped_played
    position: int | None = None
    status: int | None = None


def decide(patreon: PatreonPlayback, pc_status: int | None, pc_position: float | None,
           duration_secs: float | None) -> Decision | None:
    """Per-episode rules (spec section 7). None means there is nothing to sync."""
    if pc_status == PC_PLAYED:
        return Decision("skipped_played")
    if patreon.played:
        end = duration_secs if duration_secs is not None else patreon.position_secs or 0
        return Decision("mark_played", position=int(end), status=PC_PLAYED)
    if patreon.position_secs is None:
        return None
    if patreon.position_secs > (pc_position or 0) + AHEAD_THRESHOLD_SECS:
        return Decision("set_position", position=math.floor(patreon.position_secs), status=PC_IN_PROGRESS)
    return Decision("skipped_behind")
