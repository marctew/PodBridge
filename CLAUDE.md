# PodBridge

Syncs Patreon and YouTube playback progress into Pocket Casts. Flask + SQLite, server-rendered Jinja, vanilla JS. Read README.md and docs/spec.md before changing behaviour.

Built to `docs/design/APP-STANDARD.md` and styled to `docs/design/DESIGN-SPEC.md` (from `C:/ClaudeCowork/Design Uniformity`). Follow both; no one-off styles or layouts. PodBridge is Media category (the default violet). Its own styles are in `podbridge/static/style.css`; `podbridge/static/design/`, `podbridge/templates/macros/`, `podbridge/templates/partials/`, `podbridge/du_flask.py` and `docs/design/` are copied in by the design updater, so never edit them here.

- Runs on LXC 456 as the `podbridge` systemd service (gunicorn, one worker: the scheduler runs in-process). Data in `/var/lib/podbridge`, secrets in `/etc/podbridge.env`. Deploy with `sudo scripts/update.sh --pull`; `/healthz` needs no login.
- Every POST needs the CSRF token, as the `csrf_token` form field or the `X-CSRF-Token` header.
- Tests: `.venv/bin/python -m pytest`. No test touches the network.
- Home screen icons follow `docs/design/PWA-ICONS.md`: edit `podbridge/static/img/icon.svg` and `static/brand/logo.svg` together, then `python tools/make_icons.py podbridge/static` and commit the PNGs. The old `/static/icons/…`, `/static/manifest.webmanifest` and `/favicon.ico` URLs redirect (public) for phones that installed it earlier.
