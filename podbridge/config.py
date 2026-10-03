"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

FERNET_KEY_HELP = (
    'Generate one with: python3 -c "import base64,os; '
    'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    app_password: str
    secret_key: str
    encryption_key: str
    database_path: Path
    tz: str = "Europe/London"
    session_cookie_secure: bool = False
    login_failure_delay: float = 1.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        missing = [k for k in ("APP_PASSWORD", "SECRET_KEY", "ENCRYPTION_KEY") if not env.get(k)]
        if missing:
            raise ConfigError(f"Missing required settings: {', '.join(missing)}. See .env.example.")
        config = cls(
            app_password=env["APP_PASSWORD"],
            secret_key=env["SECRET_KEY"],
            encryption_key=env["ENCRYPTION_KEY"],
            database_path=Path(env.get("DATABASE_PATH", "/data/podbridge.db")),
            tz=env.get("TZ", "Europe/London"),
            session_cookie_secure=env.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise ConfigError("SECRET_KEY must be at least 32 characters.")
        try:
            Fernet(self.encryption_key.encode())
        except ValueError as exc:
            raise ConfigError(f"ENCRYPTION_KEY is not a valid Fernet key. {FERNET_KEY_HELP}") from exc
