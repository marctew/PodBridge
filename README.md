# PodBridge

Syncs playback progress from Patreon into Pocket Casts, so a bonus episode watched on Patreon shows as in progress or played in your podcast app. Self-hosted, single user. See [docs/spec.md](docs/spec.md).

**Status:** Phase 1 (skeleton: login, encrypted settings, database, Docker). The sync itself isn't built yet.

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
