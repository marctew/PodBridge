# Pocket Casts API notes (source-verified, 2026-10-03)

Checked against the official apps' source. Corrects spec section 5 where it differs.
Items marked **unconfirmed** must be settled by `scripts/probe_pocketcasts.py` or live testing.

Sources:
- Android: `Automattic/pocket-casts-android`, `modules/services/servers/src/main/java/au/com/shiftyjelly/pocketcasts/servers/sync/` (`SyncService.kt`, `SyncServiceManager.kt`, `login/*.kt`, `PodcastEpisodesRequest/Response.kt`, `EpisodeSyncRequest.kt`), protobuf in `modules/services/protobuf/src/main/proto/sync_api.proto`
- iOS: `Automattic/pocket-casts-ios`, `Modules/Sources/PocketCastsServer/`
- Community JSON client: `essoen/PocketCasts-mcp`, `src/api-client.ts`

## Key caveat

The official apps now send many sync calls as **protobuf** (`application/octet-stream`), including `/user/podcast/list`, `/user/sync/update`, `/up_next/sync`, and on iOS `/user/login` and `/sync/update_episode`. The JSON forms are confirmed only where noted.

## Hosts

- `https://api.pocketcasts.com`: sync, auth, user state
- `https://cache.pocketcasts.com`: podcast/episode metadata, no auth (`podcast-api.pocketcasts.net` is staging)

## Auth

- **`POST /user/login_pocket_casts`** `{email, password, scope}`, JSON on Android.
  - Response: `{email, uuid, isNew, accessToken, refreshToken, tokenType, expiresIn}`.
  - Scopes in source: `mobile`, `tv`, `watch`.
- **Legacy `POST /user/login`**, same body. Returns `{token, uuid}`. Community clients use `scope: "webplayer"`.
- **Refresh: `POST /user/token`** `{grant_type: "refresh_token", refresh_token}`. Returns the same shape as `login_pocket_casts`.
- On HTTP 401, both apps get a new token and retry once.
- **Token lifetime is unconfirmed.** The server returns it in `expiresIn`.
- Headers:
  - `Authorization: Bearer <accessToken>`
  - `User-Agent: Pocket Casts`
  - `X-App-Language` is optional
- **Rate limits are unconfirmed.** Neither app handles 429.

## Subscriptions: `POST /user/podcast/list`

- **Android (protobuf):** body `{v: "2", m: "mobile"}`. `title`, `url` and `is_private` were removed from the protobuf schema.
- **JSON (community client):** body `{v: 1}`. Response `podcasts[]` with `{uuid, title, author, url, ...}`.
- **Unconfirmed:** whether private feeds appear here and how they're marked.

## Episode user state: `POST /user/podcast/episodes` `{uuid}` (JSON, confirmed)

- Response: `{episodesSortOrder, autoStartFrom, subscribed, episodes[]}`.
- Each episode: `{uuid, duration, playingStatus, playedUpTo, isDeleted (= archived), starred}`.
- `playingStatus`: 1 = not played, 2 = in progress, 3 = completed (confirmed).
- `playedUpTo` is in seconds.
- These records have no titles or dates. Those come from the metadata endpoint below.

## Metadata: `GET https://cache.pocketcasts.com/mobile/podcast/full/{podcastUuid}`

- Response: `{episode_count, has_more_episodes, podcast: {uuid, title, url, is_private, episodes[]}}`.
- Each episode: `{uuid, title, duration (float), published (string), season, number, file_type}`.
- **Unconfirmed:** whether private feeds are served. The `is_private` field suggests they are.
- `has_more_episodes` means the response is paginated for long feeds. Handle this.

## Update: `POST /sync/update_episode` (JSON on Android)

- Android always sends all five fields: `{uuid, podcast, position, duration, status}`.
  - Position, duration and status are integers. Position and duration are in seconds.
- **Progress:** `status=2`, `position=<secs>`.
- **Mark played:** `status=3`, `position=duration`, `duration=duration`.
- **Unconfirmed:** whether partial bodies work (the community client sends them). PodBridge should always send all five fields.
- The only bulk endpoint is protobuf (`/user/sync/update`). There's no JSON equivalent.

## Star: `POST /sync/update_episode_star` (JSON, web player)

- Body: `{uuid, podcast, star}`, where `star` is a boolean. Taken from the web player's `saveEpisodeStar` (static.pocketcasts.com/webplayer/assets/api-*.js, October 2026).
- Sending `starred` to `/sync/update_episode` (a community client's form) does nothing.
- Starred state reads back as `starred` on `/user/podcast/episodes` items.
