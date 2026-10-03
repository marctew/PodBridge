# PodBridge — build spec

Working title. A small self-hosted web app that reads my playback progress from Patreon and writes it into Pocket Casts, so an episode I watch on Patreon shows as in-progress or played in my podcast app.

## 1. Goal

I listen to podcasts in Pocket Casts (iPhone, iPad, Windows, Volvo AAOS). One show, Button Boys, has Patreon-only bonus episodes that I sometimes watch as video on Patreon and sometimes listen to in Pocket Casts via the private RSS feed. The two don't share playback state. PodBridge closes that gap, one way: **Patreon -> Pocket Casts**.

Success looks like: I watch 20 minutes of a bonus episode on Patreon, and within about 15 minutes Pocket Casts shows that episode at the same position. If I finish it on Patreon, Pocket Casts marks it played.

### Non-goals (v1)

- No writing back to Patreon. Do not call Patreon's `tracking` endpoint. The reverse direction is handled with timestamped links instead (section 9a).
- No downloading or re-hosting of media.
- No multi-user support. One Patreon account, one Pocket Casts account.

## 2. Stack and deployment

- Python 3.12, Flask, SQLite, APScheduler (in-process).
- Server-rendered UI (Jinja + a little vanilla JS or htmx). No SPA build step.
- Docker Compose on an Ubuntu 24.04 LXC (Proxmox). Single container, one volume for the SQLite file.
- Add `dns: [1.1.1.1, 8.8.8.8]` to the service in `docker-compose.yml` (I've needed this on other LXC Docker apps).
- Listens on one HTTP port (pick something unused, e.g. 7330). It will sit behind my reverse proxy and is only reached over LAN/Tailscale. Do not assume public exposure, but do add a simple single-password login (password from env) so it isn't wide open.
- Config via `.env`: `APP_PASSWORD`, `SECRET_KEY`, `ENCRYPTION_KEY`, `TZ=Europe/London`.

## 3. Secrets handling

Two credentials, both entered in the Settings page, stored encrypted in SQLite (Fernet, key from `ENCRYPTION_KEY`):

1. Patreon `session_id` cookie value.
2. Pocket Casts email and password (or just the bearer token after first login, refreshed as needed).

Rules:

- Never log these values, never render them back in the UI (show "set" / "not set" and last-verified time only).
- Redact `Cookie` and `Authorization` headers in any debug logging.
- Signed media URLs and tokens in Patreon responses must not be stored or logged.

## 4. Patreon side (internal web API, cookie auth)

The official OAuth API is creator-only and has no playback data. PodBridge uses the same internal JSON:API endpoints the website calls, authenticated with my own session cookie. This is undocumented and may change, so isolate it in one client module with defensive parsing.

Request basics:

- Base: `https://www.patreon.com`
- Header: `Cookie: session_id=<value>`
- Send a normal desktop browser `User-Agent` (Patreon is behind Cloudflare).
- Be gentle: sequential requests, 1 to 2 seconds apart, back off on 429 or 403.

### 4.1 Verified by me in the browser

**Media record, includes progress (logged in):**

`GET /api/media/{media_id}`

```json
{
  "data": {
    "id": "755348961",
    "type": "media",
    "attributes": {
      "display": {
        "duration": 2837.84,
        "progress": {
          "position_secs": 381.98,
          "updated_at": "2026-10-03T12:21:00.628+00:00",
          "is_watched": false,
          "watch_state": "is_watching"
        }
      },
      "media_type": "video",
      "owner_id": "171048709",
      "owner_type": "post"
    }
  }
}
```

- `watch_state` values seen: `is_not_watched`, `is_watching`. Presumably also a watched value; treat `is_watched: true` as the source of truth for played.
- `display.default_thumbnail.position` is a thumbnail timestamp, not playback. Ignore it.

**Logged-out shape (session dead):** the same endpoint still returns 200, but `progress` is just `{"is_watched": false, "watch_state": "is_not_watched"}` with no `position_secs`, and `display.url` is absent. So a 200 does not prove the session is valid.

**Post list for a collection:**

```
GET /api/posts
  ?fields[post]=title,post_type,published_at,url,media_file_duration_seconds,thumbnail
  &filter[campaign_id]=14434926
  &filter[collection_id]=1909234
  &filter[is_suspended]=false
  &filter[include_drops]=true
  &filter[is_published]=true
  &sort=collection_order
  &page[size]=50
  &json-api-version=1.0
  &json-api-use-default-includes=false
  &json-api-use-default-fields=false
```

Paginates with `page[cursor]`. Returns posts with `post_type: "podcast"` and duration in seconds.

**Single post:** `GET /api/posts/{post_id}` returns title, `published_at`, `post_type`, `post_metadata` (`season`, `episode_type`, `episode_number`), and `current_user_can_view`.

**Known IDs (Button Boys):**

| Thing | Value |
|---|---|
| campaign_id | 14434926 |
| collection_id (believed to be Hidden Cache) | 1909234 |
| sample post_id | 171048709 ("Hidden Cache - Try Not to Peep") |
| sample media_id | 755348961 |

**Session cookie:** `session_id`, set on `www.patreon.com`, one-year expiry from login. Killed by logout, password change, or Patreon revoking it.

### 4.2 Unverified, probe these first (Phase 0)

1. **Bulk progress.** Does adding `post_file` (or another field/include) to `fields[post]` on the list endpoint return each post's media ID and `progress` in one call? If yes, a full catalogue sweep is two or three requests and the per-media calls are unnecessary.
2. **Post to media mapping.** If bulk doesn't work, where does a logged-in `GET /api/posts/{id}` expose the main media ID (`post_file.media_id`, a `media` relationship, or similar)?
3. **Session check.** Confirm `GET /api/current_user` returns my user when logged in and something clearly different when not. Use it as the health check before every sync run.
4. **Collections discovery.** The site calls `/api/collection?...fields[collection]=title,...`. Find the query that lists a campaign's collections so the UI can offer them instead of me typing IDs.
5. **Audio-only posts.** Check that audio posts expose the same `progress` shape.

Write `scripts/probe_patreon.py` that takes the cookie from an env var, runs these checks, and prints only field paths and booleans (no tokens, no URLs).

## 5. Pocket Casts side (unofficial API)

Same API the web player uses. Undocumented. The mobile apps are open source (`Automattic/pocket-casts-android`, `Automattic/pocket-casts-ios`), so **verify every path and payload below against the source before relying on it**. These are from memory:

- Base: `https://api.pocketcasts.com`
- `POST /user/login` with `{email, password, scope: "webplayer"}` -> bearer token
- `POST /user/podcast/list` -> subscriptions (find the private Button Boys feed and its podcast UUID)
- `POST /user/podcast/episodes` with `{uuid}` -> per-episode state: `playingStatus`, `playedUpTo`, `duration`
- `POST /sync/update_episode` with `{uuid, podcast, status, position}`; status 1 = unplayed, 2 = in progress, 3 = played
- Episode titles/metadata for a podcast may need a separate call (the cache/podcast-api host). Private feeds may behave differently from public ones. Confirm.

Write `scripts/probe_pocketcasts.py` equivalent to the Patreon probe.

## 6. Data model (SQLite)

- `sources`: id, campaign_id, collection_id, label, pocketcasts_podcast_uuid, enabled
- `episodes`: id, source_id, patreon_post_id, patreon_media_id, title, published_at, duration_secs, pocketcasts_episode_uuid (nullable), match_method (`auto_title`, `auto_duration`, `manual`, `none`)
- `progress`: episode_id, patreon_position_secs, patreon_is_watched, patreon_watch_state, patreon_updated_at, pocketcasts_position_secs, pocketcasts_status, last_synced_at
- `sync_runs`: id, started_at, finished_at, tier (`fast`, `full`, `manual`), episodes_checked, episodes_updated, status, error
- `sync_events`: id, run_id, episode_id, action (`set_position`, `mark_played`, `skipped_behind`, `no_match`), detail
- `settings`: key, value (encrypted where secret)

Seed `sources` with the Button Boys campaign and collection above, but nothing should be hard-coded to that show.

## 7. Sync logic

### Matching

Patreon post <-> Pocket Casts episode in the private feed. Both originate from the same Patreon posts, so:

1. Exact title match (normalised: trim, collapse whitespace, casefold).
2. Tiebreak or fallback: publish date within 24 hours and duration within 5 seconds.
3. Otherwise leave unmatched and show it in the UI for manual mapping. Never guess.

Note: Patreon's private RSS includes only posts the creator added to the podcast, and video posts arrive as audio-only. Some Patreon posts will legitimately have no Pocket Casts counterpart.

### Schedule

- **Fast tier, every 15 minutes:** newest 5 posts per source, plus every episode whose last known state is `is_watching`.
- **Full tier, nightly (and on demand):** every episode in every enabled source. Anything found `is_watching` joins the fast tier.
- If Phase 0 shows bulk progress works, collapse both tiers into one full sweep every 15 minutes.

Intervals configurable in Settings.

### Per-episode rules

1. Run the session health check first. If the Patreon session is dead, abort the run, record it, raise the alert (section 9). Do not treat logged-out defaults as real progress.
2. Skip if `patreon_updated_at` hasn't changed since last sync.
3. Read current Pocket Casts state for the matched episode.
4. **Never rewind.** Only write if Patreon's position is ahead of Pocket Casts' by more than 15 seconds.
5. **Played wins.** If Patreon `is_watched` is true, mark played (status 3). If Pocket Casts already says played, do nothing, whatever Patreon says.
6. Otherwise set status 2 with `position = floor(position_secs)`.
7. Record a `sync_event` either way, including skips.

Add a global **dry-run** toggle (default on for first run) that does everything except the Pocket Casts write and logs what it would have done.

## 8. Web UI

Keep it plain and fast. Dark theme by default.

- **Dashboard:** connection status for both services (ok / expired / not set, last verified), last run summary, next run time, "Sync now" button, dry-run indicator, count of unmatched episodes.
- **Episodes:** table per source: title, published, duration, Patreon state and position, Pocket Casts state and position, match status, last synced. Filters: in progress, unmatched, recently changed. Row action: manual match (pick from that podcast's Pocket Casts episodes), unlink, force sync.
- **Activity:** sync runs with expandable event lists.
- **Sources:** add/edit campaign + collection + target Pocket Casts podcast. Use discovery endpoints if Phase 0 finds them, otherwise plain ID fields.
- **Settings:** Patreon cookie (write-only field, "Test" button), Pocket Casts credentials (write-only, "Test"), intervals, dry-run, alert webhook URL.

Also expose `GET /healthz` (no auth, returns app up + whether both sessions were valid at last check, no detail).

## 9. Alerts

When the Patreon session check fails, or Pocket Casts login fails, or three consecutive runs error:

- Show a banner on every page.
- POST a small JSON payload to an optional webhook URL (I'll point it at Home Assistant or ntfy). One alert per state change, not one per run.

## 9a. Resume links (the reverse direction, without writing to Patreon)

Patreon post URLs accept a timestamp parameter that starts the video at a given point (I confirmed this in the browser). That gives a safe way to carry progress from Pocket Casts back to Patreon: PodBridge never changes Patreon's stored position, it just builds a link that opens the video where I actually am.

- Put the parameter name and format in one constant, `PATREON_TIMESTAMP_PARAM` (**TODO: fill in the exact one I confirmed, e.g. `t`, and whether it takes plain seconds**).
- **Resume position** for an episode = the further of the Patreon position and the Pocket Casts position, unless either side says played.
- **Episodes page:** each matched, unfinished row gets a "Continue on Patreon" link: the post URL plus the timestamp parameter set to `floor(resume position)`.
- **Dashboard:** a "Continue watching" list of in-progress episodes, most recently touched first, each with that link.
- **Stable redirect endpoints** (behind the app login), so I can bookmark them or call them from an iOS Shortcut:
  - `GET /go/<episode_id>` -> 302 to the Patreon URL at the resume position.
  - `GET /go/latest` -> same, for the most recently touched in-progress episode.
  - These must read fresh Pocket Casts state at click time, not the last sync's cached value.
- **To verify:** whether the parameter is honoured when the link opens in the Patreon iOS/iPadOS app (universal link) or only in a browser. If the app ignores it, note that in the UI next to the link.

## 10. Build order

0. **Probes** (sections 4.2 and 5). Stop and report findings before building further; they decide whether the per-media tier exists and how matching data is fetched.
1. Skeleton: Flask app, login, SQLite schema + migrations, Settings with encrypted secrets, Docker Compose.
2. Patreon client + session health check + episode discovery into the DB.
3. Pocket Casts client + podcast/episode fetch + auto-matching + manual match UI.
4. Sync engine with dry-run, scheduler, Activity page.
4a. Resume links and `/go/` redirects (section 9a).
5. Alerts, `/healthz`, polish, README with deploy and "how to get the cookie" steps.

## 11. Testing

- Unit tests for resume-position selection and link building (section 9a).
- Unit tests for matching and the per-episode rules (never rewind, played wins, unchanged skip, logged-out detection) using recorded fixture JSON with all tokens and URLs stripped.
- Both API clients behind interfaces so the sync engine tests run with fakes.
- No test may make a live network call.

## 12. Later, not now

- More Patreon campaigns/collections (should already work via Sources).
- YouTube watch history -> Pocket Casts for shows I watch on YouTube.
- A single "unplayed across everything" view.
- A small JSON API so Home Assistant can show "currently watching".
