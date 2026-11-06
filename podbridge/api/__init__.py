"""PodBridge's outside API, to Design Uniformity's API-STANDARD.md (draft 4).

The shared part (the token gate, limits, ETags, image resizing, SSE and webhook signing) is
du_api.py, carried in by the design updater. This package is PodBridge's own part:

  tokens     API tokens (Settings → API tokens), stored as sha256
  events     the events table: publish, read after an id, prune
  webhooks   Settings → Webhooks and their deliveries (sent from the scheduler, never a request)
  feeds      /api/v1/status and the feeds, in the standard's shapes
  actions    /api/v1/actions/<name>, with Idempotency-Key and the action log
  web        the routes: /api/v1/*, /display/*, and the Settings cards' forms

The endpoints are documented for readers in docs/API.md.
"""

from datetime import datetime, timedelta, timezone


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso(offset_s: float = 0) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(seconds=offset_s))


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
