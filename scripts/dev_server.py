"""Local development server with throwaway config. Not for deployment.

Uses ./data/dev.db and dev-only keys stored in ./data/dev-keys.json (git-ignored).
Password: PODBRIDGE_DEV_PASSWORD, default "dev-password".
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from podbridge import create_app  # noqa: E402
from podbridge.config import Config  # noqa: E402

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
keys_file = DATA / "dev-keys.json"
if not keys_file.exists():
    keys_file.write_text(json.dumps({
        "secret_key": secrets.token_urlsafe(48),
        "encryption_key": Fernet.generate_key().decode(),
    }))
keys = json.loads(keys_file.read_text())

app = create_app(Config(
    app_password=os.environ.get("PODBRIDGE_DEV_PASSWORD", "dev-password"),
    secret_key=keys["secret_key"],
    encryption_key=keys["encryption_key"],
    database_path=DATA / "dev.db",
    login_failure_delay=0,
))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "7330")), debug=True)
