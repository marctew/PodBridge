"""Interpretation of Patreon progress for syncing.

Patreon sets `is_watched` well before the end (observed: flagged at 40:44 of
47:18, about 86%), so the flag alone does not mean "finished". Decision
(2026-10-03, spec section 7 amendment): sync the real position, and treat an
episode as played only when the position is within PLAYED_TAIL_SECS of the
end, or when Patreon says watched but gives no usable position.
"""

from __future__ import annotations

from dataclasses import dataclass

PLAYED_TAIL_SECS = 60.0


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
