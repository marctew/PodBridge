# Phase 0 findings (2026-10-03)

Probes run from the LXC. Raw output is not stored here.

## Patreon

| Question (spec 4.2) | Result |
|---|---|
| Session check | Works. `GET /api/current_user` returns 200 with `data.type = "user"` when logged in, and 401 with `errors` when logged out. Use it before every run. |
| Logged-out media shape | Confirmed. Still returns 200, but `progress` has no `position_secs` or `updated_at`. |
| Bulk progress | Works. Adding `post_file` to `fields[post]` on the list endpoint returns each post's `post_file.media_id` and `post_file.progress` (`position_secs`, `updated_at`, `is_watched`, `watch_state`). |
| Post to media mapping | `attributes.post_file.media_id` on both the list and single-post endpoints. It matches the sample media ID. Don't use `relationships.media`, which also includes attachments and previews (19 media for 5 posts). |
| Collections discovery | `GET /api/collection?filter[campaign_id]=…&fields[collection]=title,num_posts` lists them. Hidden Cache = `1909234` (63 posts) is confirmed. Button Boys has 6 collections. |
| Audio posts | **Unproven.** The audio media sampled had never been played, so only the unplayed shape was seen. Recheck once an audio post has some progress. |

Collection size: 64 posts, so a full sweep is 2 requests at `page[size]=50` with `page[cursor]` pagination.

**Design consequences:**
- Collapse the fast and full tiers into one full sweep every 15 minutes (spec 7). Per-media calls aren't needed.
- `post_file` also carries `viewer_playback_data.playback_token`, signed `url`s, storyboard and transcript URLs. The client must keep only `media_id`, `duration` and `progress`, and drop everything else as soon as it parses the response (spec 3).

## Pocket Casts

| Question | Result |
|---|---|
| Login | `POST /user/login_pocket_casts` (scope `mobile`) returns an access token and a refresh token, `tokenType` Bearer, `expiresIn` 3600. |
| Refresh | `POST /user/token` with `grant_type=refresh_token` works and returns a new refresh token, so the stored one rotates. |
| Subscriptions | JSON `POST /user/podcast/list` `{v:1}` works, with `title`, `author` and `url` included. Button Boys is found. |
| Episode state | `POST /user/podcast/episodes` returns **only episodes that have user state** (1 here), with no titles. Field types: `playedUpTo` and `duration` are ints. |
| Episode metadata | **Gap.** `cache.pocketcasts.com/mobile/podcast/full/{uuid}` returned 200 with no podcast or episodes for the private feed. |

**Open question:** where to get the private feed's full episode list (UUIDs and titles) so unplayed episodes can be matched and written. `scripts/probe_pocketcasts_feed.py` tests these candidates:
- the other cache and podcast-api paths
- `/user/episode`
- the feed's own RSS

It also checks whether more than one subscription matches "Button Boys" (public and private feeds).
