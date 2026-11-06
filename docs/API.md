# API

PodBridge's outside API follows Design Uniformity's `API-STANDARD.md` (draft 4, a copy in `docs/design/`): the same shape as Marc's other apps, for Docket, Vital Signs, Home Assistant, the ESP32 panels and TRMNL. The shared part (the token gate, limits, ETags, image resizing, event streams, webhook signing) is `podbridge/du_api.py`, carried in by the design updater. PodBridge's own part is `podbridge/api/`.

This file is the whole API. Check a running PodBridge against the standard with Design Uniformity's checker:

```bash
python3 scripts/check_api.py http://127.0.0.1:7330 <read_token> [write_token]
```

The app's own pages use their own routes with the session and CSRF; nothing here changes them.

## Tokens, for every route

- `Authorization: Bearer <token>`, made in **Settings → API tokens** (shown once, stored as a hash). Read tokens read; write tokens read and write.
- **Read tokens may go in the URL** as `?token=…`, for displays that can't set a header. A write token in the URL is refused with `400`. URL tokens never reach the access log: gunicorn logs the path without the query string, and no referrer.
- The signed-in session reads without a token, so `/display/` previews work from Settings. Its writes need the `X-CSRF-Token` header and are logged as "Admin session".
- **Limits:** 10 bad tokens from one address in a minute locks that address out for 15 minutes (`429` with `Retry-After`). Through the Cloudflare tunnel the address is `CF-Connecting-IP`. Each token gets 120 requests a minute (images and events don't count). PodBridge has one gunicorn worker, so these are exact.
- **Through the tunnel:** the API answers through Cloudflare too, always with a token. The zone needs a Configuration Rule turning Browser Integrity Check off for `/api/v1/` and `/display/` on PodBridge's hostname (API standard section 4), or readers get Cloudflare's `403` (error code 1010). `API_PUBLIC=0` in `/etc/podbridge.env` would refuse tunnel requests instead.
- **Caching:** every successful GET has a weak `ETag`; send it back as `If-None-Match` and an unchanged answer is `304` with no body. Status and feeds also send `Cache-Control: private, max-age=<refresh_s / 2>`.
- `/healthz` needs no token (and says nothing about contents).

Errors are `{"error": "<a sentence>", "code": "<code>"}` with a 4xx or 5xx status; `429` and `503` carry `Retry-After`.

## Routes

| Method | Path | Answers |
|---|---|---|
| GET | `/api/v1/meta` | Who PodBridge is and what it offers: `app` (`podbridge`), `name`, `version`, `sha`, `api` (1), `accent` (violet), `icon`, `time_zone`, and the lists `feeds`, `actions`, `displays`, `events` |
| GET | `/api/v1/status` | The one-glance summary, below |
| GET | `/api/v1/feeds/{name}` | A list in the common item shape, below |
| GET | `/api/v1/images/{kind}/{id}` | Artwork, resized here: `episode/{episode_id}` (the episode's thumbnail, else its show's art) or `show/{podcast_uuid}` |
| POST | `/api/v1/actions/{name}` | One named thing to do, below. Write token, in the header only |
| GET | `/api/v1/events` | Server-sent events, below |
| GET | `/display/continue-watching` | A ready-made screen, below |

## Status

From the database only; it never waits on Patreon, YouTube or Pocket Casts. `refresh_s` 60.

```json
{"app": "podbridge", "state": "ok", "headline": "3 in progress · last sync today 14:02",
 "detail": "Next: Russell Brand the rapist",
 "counts": [{"key": "in_progress", "label": "In progress", "value": 3},
            {"key": "new_this_week", "label": "New this week", "value": 5},
            {"key": "my_list", "label": "My List", "value": 2},
            {"key": "unmatched", "label": "Unmatched", "value": 4, "tone": "warning"},
            {"key": "written_today", "label": "Written today", "value": 7}],
 "url": "http://192.168.4.69:7330/", "generated_at": "2026-11-06T14:05:00Z", "refresh_s": 60}
```

- `state`: `busy` while a sync runs; `off` until Pocket Casts and a source are set up; `error` when the last three syncs failed; `warning` when a login has expired or isn't working (the headline says which); otherwise `ok`.
- `detail` says when dry run is on (nothing is written to Pocket Casts), then the episode at the top of Continue watching.
- `written_today`: positions and "played" written to Pocket Casts since local midnight, by real (not dry) syncs.

## Feeds

Every feed answers `{"feed", "title", "items", "next_cursor", "generated_at", "refresh_s"}` and takes `limit` (default 20, at most 100). Feeds ordered by time, newest first (`new`, `history`, `syncs`), also take `since` (ISO time: items after it) and `cursor` (from `next_cursor`). The others are answered whole up to `limit`.

| Feed | What | Order | `at` | `refresh_s` |
|---|---|---|---|---|
| `continue` | Continue watching: episodes in progress with somewhere to resume | last watched first | when last watched | 120 |
| `new` | Every episode, newest published first, whatever its state | published, newest first | published | 600 |
| `up_next` | For each show you've watched, the next unwatched episode | most recently active show first | published | 600 |
| `my_list` | My List, in your order | yours | published | 300 |
| `history` | Every progress change seen, on either side | newest first | when seen | 120 |
| `syncs` | Sync runs and what they did | newest first | started | 120 |

**Episode items** (every feed but `syncs`):

```json
{"id": "episode:1412", "title": "Russell Brand the rapist", "subtitle": "The News Agents · 18:00 left",
 "at": "2026-11-05T20:15:00Z", "at_label": "Yesterday 20:15", "tone": "accent",
 "image": "/api/v1/images/episode/1412", "url": "http://192.168.4.69:7330/episodes/1412",
 "meta": {"episode_id": 1412, "show": "The News Agents", "source": "youtube", "state": "in_progress",
          "progress_pct": 47.4, "position_secs": 1080.0, "duration_secs": 2280.0,
          "published_at": "2026-11-05T17:00:00Z", "resume_url": "https://www.youtube.com/watch?v=…&t=1080",
          "pocketcasts_url": "https://pocketcasts.com/podcasts/…/…", "matched": true, "in_list": false}}
```

- `title` drops a YouTube "| Channel name" suffix. `state` is `in_progress`, `unwatched` or `played`. `resume_url` opens the episode on Patreon or YouTube at the further of the two positions; `pocketcasts_url` opens it in Pocket Casts' web player. Fields that don't apply are left out.
- `history` items have their own `id` (`history:<n>`), `at` is when the change was seen, and `meta` adds `side` (`patreon`, `youtube` or `pocketcasts`), `watched_position_secs` and `watched_played`.

**Sync items** (`syncs`): `id` `sync:<run>`, a `title` such as "Synced: 3 written to Pocket Casts" or "Sync failed: …", `tone` (`success`, `info` while running, `warning` for a run stopped by an expired login, `danger`), `url` the Activity page, and `meta` `{run_id, status, dry_run, tier, checked, updated, finished_at, error}`.

## Actions

`POST /api/v1/actions/{name}` with a JSON object body. Answers `{"ok": true, "message": "…", "result": {…}}`, with `message` short enough for a toast or a panel line. Send an `Idempotency-Key` header (1 to 100 characters) and a repeat within 24 hours gets the first answer back and does nothing twice. Every call, refused or not, is in the action log on Settings → API tokens.

| Action | Body | Does | `result` |
|---|---|---|---|
| `sync_now` | `{}` | Starts a sync in the background (`sync.finished` or `sync.failed` follows). At most 6 an hour per token; `503 busy` if one is already running | `{"started": true}` |
| `mark_played` | `{"episode_id": int}` | Marks the episode played in Pocket Casts (written even in dry run, like the button) | `{"episode_id", "played": true}` |
| `mark_unplayed` | `{"episode_id": int}` | Back to the start, unplayed, in Pocket Casts | `{"episode_id", "played": false}` |
| `list_add` | `{"episode_id": int}` | Adds it to the bottom of My List | `{"episode_id", "in_list": true, "changed"}` |
| `list_remove` | `{"episode_id": int}` | Takes it off My List | `{"episode_id", "in_list": false, "changed"}` |

`episode_id` is in every episode item's `meta`. An unknown episode is `404`; an unmatched one can't be marked (`400`); Pocket Casts refusing is `502 upstream` (retry with the same key). Nothing destructive is offered.

## Events

`GET /api/v1/events` is a server-sent events stream (read token, URL or header). Each event is `event: <type>`, `id: <n>`, `data: {"id", "type", "at", "title", "item"}` with `item` in the common shape. Reconnect with `Last-Event-ID` (or `?last_event_id=`) to replay up to 200 missed events; without one, nothing is replayed. A `: ping` comes every 25 seconds. At most 8 streams are open at once (`503` beyond that); events are kept an hour, and always the last 200.

| Type | When | `item` |
|---|---|---|
| `episode.new` | A sync or Refresh finds an episode published in the last 14 days (a newly added source's back catalogue isn't announced) | the episode |
| `progress.synced` | A real sync writes a position or "played" to Pocket Casts | the episode; `meta.action` (`set_position` or `mark_played`) and `meta.position_secs` |
| `sync.finished` | A sync run ends without errors (dry runs too) | the sync |
| `sync.failed` | A sync run ends with an error or an expired login | the sync |
| `login.expired` | Patreon, Pocket Casts or YouTube's login is found expired (once, not on every run) | `{"id": "login:<service>", "title", "tone": "danger", "url": Settings, "meta": {"service"}}` |

**Webhooks:** Settings → Webhooks POSTs each event (the same `data`) to a URL, for Home Assistant and n8n, signed with `X-Signature: sha256=<hex HMAC-SHA256 of the body with the webhook's secret>`, plus `X-Event-Type` and `X-Event-Id`. Pick the event types to send (none ticked: all). Deliveries go out from the scheduler within a minute, with a 10-second timeout, and a failure is tried again after 1, 3 and 6 minutes. The URL is shown masked once saved.

## Display page

`GET /display/continue-watching`: artwork tiles with progress bars for what's in progress, for kiosks, a Vital Signs screen or the widget display. Read token in the URL (`?token=`); the signed-in session can preview it from Settings.

- `size=800x480` (a layout hint: up to 480 wide shows 2, up to 1100 shows 3 across, wider 4), `theme=dark|light`, `scale=0.5–3`, `accent=violet|blue|graphite|teal|fuchsia`, `static=1` to render once with no JavaScript (e-ink, screenshots).
- Unless `static=1`, it reloads its own body (`fragment=1`) every 2 minutes, and straight away on `progress.synced` or `sync.finished`. If PodBridge stops answering it keeps what it has and says when it last updated.
- Refusals are a plain HTML page, not JSON. Sent with `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and `X-Robots-Tag: noindex`.
