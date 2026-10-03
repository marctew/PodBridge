# PodBridge

Syncs playback progress from Patreon into Pocket Casts, so a bonus episode watched on Patreon shows as in progress or played in your podcast app. Self-hosted, single user. See [docs/spec.md](docs/spec.md).

**Status:** Phase 3. Done so far:
- login and encrypted settings
- Patreon discovery
- the Pocket Casts client
- automatic and manual episode matching
- the sync engine with dry run, the scheduler, **Sync now** (which restarts the countdown), and the Activity page

## YouTube channels (experimental)

A YouTube channel can be a source too, for example `@TheNewsAgents` linked to its podcast in Pocket Casts.

**How it works:**
- PodBridge reads your YouTube watch history (the red progress bar), so positions are accurate to about 1%.
- Video titles and lengths differ from the podcast's, so it matches on publish date (within 30 hours), using title similarity as a tiebreak.
- If the podcast has inserted ads, its timeline may not line up exactly with the video.

**Setup:**
1. Open a private window, sign in to YouTube, and export youtube.com cookies as `cookies.txt`. Close the window without signing out.
2. Paste the cookies into Settings → YouTube and click **Save & test**.
3. On the Sources page, use **Add a YouTube channel**, then **Link podcast**.

**To check the parsing works on your history first** (read-only):

```bash
docker compose exec -e YOUTUBE_COOKIES_FILE=/tmp/cookies.txt podbridge python scripts/probe_youtube.py @TheNewsAgents
```

Copy the file into the container first, with `docker compose cp`.

Dry run is on by default. Turn it off in Settings once the Activity log looks right. Next is Phase 4a (resume links) and Phase 5 (alerts and polish).

## Deploy (Docker Compose)

```bash
git clone https://github.com/marctew/PodBridge.git /opt/podbridge
cd /opt/podbridge
cp .env.example .env && chmod 600 .env
nano .env
docker compose up -d --build
```

In `.env`, set `APP_PASSWORD`, `SECRET_KEY` and `ENCRYPTION_KEY`. The commands to generate the two keys are in the file.

The app listens on port **7330**. Open `http://<lxc-ip>:7330`, log in, and add credentials under **Settings**.

**Update:**

```bash
cd /opt/podbridge && git pull && docker compose up -d --build
```

**Data:** the SQLite database lives in the `podbridge-data` Docker volume. Keep a copy of `ENCRYPTION_KEY`; without it, the stored credentials can't be decrypted and must be re-entered.

**Health:** `GET /healthz` needs no login and reports only whether each session was valid at the last check.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest
.venv/bin/python scripts/dev_server.py          # http://127.0.0.1:7330
```

The dev server uses `./data/` with throwaway keys. Its password is `PODBRIDGE_DEV_PASSWORD`, which defaults to `dev-password`.
