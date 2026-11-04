#!/usr/bin/env bash
# Install PodBridge on an Ubuntu LXC as a systemd service. Safe to re-run. Run as root from the checkout:
#   bash deploy/install.sh
# It makes the podbridge user, the venv, /var/lib/podbridge and the unit. The secrets file
# /etc/podbridge.env is yours to fill in from deploy/podbridge.env.example; this never overwrites it.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ "$REPO" = /opt/podbridge ] || { echo "The unit expects the checkout at /opt/podbridge." >&2; exit 1; }

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git

echo "==> User and folders"
id podbridge >/dev/null 2>&1 || useradd --system --home-dir /var/lib/podbridge --shell /usr/sbin/nologin podbridge
install -d -o podbridge -g podbridge -m 750 /var/lib/podbridge

echo "==> Python environment"
[ -x "$REPO/.venv/bin/python" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
sha256sum "$REPO/requirements.txt" | cut -d' ' -f1 > "$REPO/.venv/.requirements.sha256"

echo "==> Secrets"
if [ ! -f /etc/podbridge.env ]; then
    install -m 600 "$REPO/deploy/podbridge.env.example" /etc/podbridge.env
    echo "    Wrote /etc/podbridge.env from the example. Fill it in, then run this again."
    exit 0
fi
chmod 600 /etc/podbridge.env

echo "==> systemd"
install -m 644 "$REPO/deploy/podbridge.service" /etc/systemd/system/podbridge.service
systemctl daemon-reload
systemctl enable -q podbridge
bash "$REPO/scripts/update.sh" --force
echo "PodBridge is running on http://$(hostname -I | awk '{print $1}'):7330. Logs: journalctl -u podbridge -f"
