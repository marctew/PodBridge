"""Resume links: the reverse direction without writing to Patreon (spec section 9a).

The resume position is the further of the Patreon and Pocket Casts positions,
unless either side says played. The link is the Patreon post URL with the
timestamp parameter set to whole seconds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .rules import PC_PLAYED, interpret_patreon

DEFAULT_TIMESTAMP_PARAM = "t"
TIMESTAMP_PARAM_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,20}$")


@dataclass(frozen=True)
class Resume:
    position_secs: float
    source: str  # "patreon" | "pocketcasts" | "start"


def resume_position(patreon_position: float | None, patreon_is_watched: bool, duration_secs: float | None,
                    pocketcasts_status: int | None, pocketcasts_position: float | None) -> Resume | None:
    """None when either side says played."""
    patreon = interpret_patreon(patreon_position, patreon_is_watched, duration_secs)
    if patreon.played or pocketcasts_status == PC_PLAYED:
        return None
    p = patreon.position_secs or 0.0
    pc = pocketcasts_position or 0.0
    if p <= 0 and pc <= 0:
        return Resume(0.0, "start")
    return Resume(pc, "pocketcasts") if pc > p else Resume(p, "patreon")


def build_resume_url(post_url: str, position_secs: float, param: str = DEFAULT_TIMESTAMP_PARAM) -> str:
    parts = urlsplit(post_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    secs = math.floor(position_secs)
    if secs > 0:
        query.append((param, str(secs)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def last_touched(patreon_updated_at: str | None, pocketcasts_changed_at: str | None) -> datetime | None:
    times = [t for t in (parse_time(patreon_updated_at), parse_time(pocketcasts_changed_at)) if t]
    return max(times) if times else None
