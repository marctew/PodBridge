# PodBridge

PodBridge syncs your viewing progress from **Patreon** and **YouTube** into **Pocket Casts**. If you watch 20 minutes of a show's video, its podcast episode in Pocket Casts picks up at 20:00. Finish it, and Pocket Casts marks it played.

It also goes the other way: **Continue** links open Patreon or YouTube at the further of your two positions.

It's self-hosted, for one user, and has a Plex-style web interface.

- [What it does](#what-it-does)
- [Deploy](#deploy)
- [First-time setup](#first-time-setup)
- [Getting your logins](#getting-your-logins)
- [Day to day](#day-to-day)
- [Updating](#updating)
- [Backups](#backups)
- [Troubleshooting](#troubleshooting)
- [How syncing decides](#how-syncing-decides)
- [Development](#development)

---

## What it does

- **Sources:**
  - Patreon campaigns, whole or one collection.
  - YouTube channels.

  Each is linked to one or more Pocket Casts podcasts. One YouTube channel can feed two shows (for example The News Agents and The News Agents USA): videos are routed by the "| Show name" at the end of their titles.
- **Matching:** pairs each source episode with its Pocket Casts episode.
  - Patreon: by exact title, or date plus length.
  - YouTube: by title, date and similarity.

  Anything unclear is left for you to match by hand on the **Matching** page. It never guesses.
- **Syncing:** runs every 15 minutes (adjustable), and **Sync now** runs one immediately and restarts the countdown.
  - It never rewinds Pocket Casts.
  - It never overrides "played" in Pocket Casts.
  - It marks played only when you're within the last minute.
- **Library, Home and History:**
  - **Library:** show grid with series art, and search.
  - **Home rows:** Continue watching, Up next, Recently watched and Recently added.
  - **History:** what you watched or listened to, by day.
- **On your phone:**
  - **Bottom tab bar:** it's laid out for phones.
  - **Open with:** tapping a thumbnail asks whether to watch the video or listen in Pocket Casts. On iPhone both open the apps directly.
  - **Home Screen:** add it to your Home Screen for a full-screen app with its own icon.
- **Housekeeping:**
  - **Mark played/unplayed:** per episode.
  - **Catch-up:** marks an episode and everything older played, with undo.
  - **Hide:** hides episodes you don't care about.
  - **Problem banner:** appears when a login expires.
  - **Backup and restore:** of the whole database.

## Deploy

PodBridge runs as one Docker container. This assumes a Linux host (for example an Ubuntu LXC on Proxmox) with Docker and Compose installed.

```bash
git clone https://github.com/marctew/PodBridge.git /opt/podbridge
cd /opt/podbridge
cp .env.example .env && chmod 600 .env
nano .env
docker compose up -d --build
```

In `.env`, set:

| Setting | What it is |
|---|---|
| `APP_PASSWORD` | The password for the web interface. |
| `SECRET_KEY` | Random, 32+ characters. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ENCRYPTION_KEY` | Encrypts your stored logins. Generate with `python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`. **Keep a copy somewhere safe**; see [Backups](#backups). |
| `TZ` | Your time zone, for example `Europe/London`. |
| `SESSION_COOKIE_SECURE` | `true` if you only reach PodBridge over HTTPS. |

PodBridge listens on port **7330**. Put it behind your reverse proxy, or open `http://<host>:7330`.

The compose file pins DNS to `1.1.1.1` / `8.8.8.8`, because some LXC setups resolve unreliably.

## First-time setup

1. **Log in** with `APP_PASSWORD`.
2. **Settings:** enter your Patreon cookie and Pocket Casts login, then press **Save & test** for each. The YouTube cookies are only needed for YouTube channels. See [Getting your logins](#getting-your-logins).
3. **Sources:**
   - The Button Boys Patreon is set up as an example. Use **Widen to all posts** to cover the whole campaign, or add your own source.
   - For YouTube, use **Add a YouTube channel** (for example `@TheNewsAgents`).
   - For each source, **Link podcast** and tick the Pocket Casts podcasts it feeds.
4. **Matching:** press **Refresh**, then check the **Unmatched** filter. Fix anything with the Match link: there's a filter box, and **Move here** steals a wrong match.
5. **Dry run** is on to start with. Press **Sync now** and read **Activity**: it lists what each sync *would* do. When that looks right, untick **Dry run** in Settings.
6. **Optional:** on your iPhone, open PodBridge in Safari, then Share → **Add to Home Screen**.

## Getting your logins

### Patreon cookie

1. On a computer, log in at patreon.com.
2. Open the developer tools (F12), go to **Application** (Chrome or Edge) or **Storage** (Firefox), then **Cookies** → `https://www.patreon.com`.
3. Copy the value of `session_id` into Settings → Patreon. Pasting `session_id=…` also works.

The cookie lasts about a year, but logging out of Patreon or changing your password kills it. PodBridge shows a red banner when that happens.

### YouTube cookies

YouTube's API can't read watch progress, so PodBridge reads your watch history using your login cookies.

1. **Use Firefox.** Chrome on Windows ties Google logins to the PC, so cookies exported from it stop working elsewhere within hours.
2. Open a **private window** (Ctrl+Shift+P), sign in to YouTube and play a few seconds of any video.
3. Export youtube.com's cookies in `cookies.txt` format with a cookies.txt extension. Allow it to run in private windows.
4. **Close the private window without signing out.**
5. Paste the file's contents into Settings → YouTube, then **Save & test**.

Don't use that login anywhere else (another tool, a script, a cron job). Google rotates the cookies, and two copies knock each other out. PodBridge saves each rotation, and Settings shows how long the current login has lasted.

Publish dates are read from public watch pages, without your login.

### Pocket Casts

Your normal email and password. PodBridge keeps a refresh token, so it doesn't log in every time.

## Day to day

- **Home:** Continue watching, Up next, Recently watched and Recently added, plus connection and sync status.
- **Library:** shows and search. On a show page:
  - **Filters:** All, In progress, Unwatched, Played and Hidden.
  - **Per episode:**
    - **↻ Sync now:** fresh progress from both sides, then the normal rules.
    - **✓ Mark played** or **Mark unplayed:** writes to Pocket Casts immediately.
    - **⏪ This & older played:** catch-up for backlogs, with **↶ Undo**.
    - **Hide.**
  - **Hide N unmatched:** clears out posts that will never be in the podcast.
- **Matching:** every episode with its match. Click the Match column to change one.
- **Activity:** every sync run, and what it did to each episode.
- **History:** day by day, what you watched or listened to, and how far.
- **`/go/latest`:** bookmark it, or use it from an iOS Shortcut. It opens the most recently touched in-progress episode at the right point.

## Updating

```bash
cd /opt/podbridge && git pull && docker compose up -d --build
```

Database changes are applied automatically on start.

## Backups

- **Database:** Settings → **Download backup** saves your sources, matches, progress, history and settings as one `.db` file. **Restore** in the same place swaps one back in.
- **`ENCRYPTION_KEY`:** your logins inside the backup are encrypted with it, so keep a copy of `.env` (or just that line) somewhere safe.
  - **Key lost:** a restore can still bring everything back **without credentials**, and you re-enter them.
- **Artwork:** not backed up. It's downloaded again as needed.

The live database is in the `podbridge-data` Docker volume. To copy it from the host:

```bash
docker compose cp podbridge:/data/podbridge.db ./podbridge-copy.db
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| Red banner: "Patreon session has expired" | Paste a fresh `session_id` cookie. |
| Red banner: "YouTube login has expired" | Export fresh cookies from a Firefox private window. If it keeps happening after a few hours, check nothing else uses that login. |
| Red banner: "The last 3 syncs failed" | Open **Activity** for the error. Usually a temporary block (HTTP 429), which clears up on its own. |
| Episodes unmatched | **Matching → Unmatched**, then click **Unmatched** to match by hand. Hide ones that aren't in the podcast. |
| YouTube shows "publish date not fetched yet" | Dates fill in on scheduled syncs, or press **Fetch missing dates now** on the Sources page. |
| Positions in Pocket Casts don't line up with the video | Podcasts with inserted ads are longer than the video, so positions drift by the ad length. |
| iPhone shows old styling after an update | Pull down to reload once. |
| "A sync is already running" | Wait a minute. Only one sync runs at a time. |

`/healthz` needs no login and reports whether the app is up and each login was valid at the last check. You can point an uptime monitor at it.

## How syncing decides

For each matched episode whose source progress changed since the last run:

1. **Played in Pocket Casts already:** left alone.
2. **Within the last minute on the source:** marked played. Patreon's own "watched" flag fires early (around 85%), so it isn't trusted alone.
3. **Source ahead of Pocket Casts by more than 15 seconds:** Pocket Casts is set to that position.
4. **Otherwise:** skipped, because PodBridge never rewinds.

The full design is in [docs/spec.md](docs/spec.md), and what was learned about the APIs is in [docs/phase0-findings.md](docs/phase0-findings.md) and [docs/pocketcasts-api.md](docs/pocketcasts-api.md).

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest                      # no test touches the network
.venv/bin/python scripts/dev_server.py          # http://127.0.0.1:7330
```

**The dev server:**
- **Data:** uses `./data/` with throwaway keys.
- **Password:** `PODBRIDGE_DEV_PASSWORD`, which defaults to `dev-password`.
- **Scheduler:** it doesn't run one.

**Scripts:**

| Script | Purpose |
|---|---|
| `scripts/probe_*.py` | Read-only checks against the real services (see `docs/phase0-findings.md`). |
| `scripts/probe_youtube.py` | Run inside the container to test YouTube parsing with PodBridge's stored login. |
| `scripts/make_icons.py` | Regenerates the app icons (needs Pillow). |
