"""Config, encryption, redaction, migrations and the settings store."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from cryptography.fernet import Fernet

from podbridge.config import Config, ConfigError
from podbridge.crypto import SecretBox, SecretError
from podbridge.db import connect, migrate, migrations
from podbridge.redact import REDACTED, RedactingFilter, redact, register_secret
from podbridge.settings_store import SettingsStore


def test_config_requires_core_values():
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        Config.from_env({"SECRET_KEY": "x" * 40, "ENCRYPTION_KEY": Fernet.generate_key().decode()})


def test_config_rejects_bad_fernet_key():
    with pytest.raises(ConfigError, match="Fernet"):
        Config.from_env({"APP_PASSWORD": "p", "SECRET_KEY": "x" * 40, "ENCRYPTION_KEY": "nope"})


def test_secretbox_roundtrip_and_wrong_key():
    box = SecretBox(Fernet.generate_key().decode())
    token = box.encrypt("hunter2-value")
    assert "hunter2" not in token
    assert box.decrypt(token) == "hunter2-value"
    with pytest.raises(SecretError):
        SecretBox(Fernet.generate_key().decode()).decrypt(token)


@pytest.mark.parametrize("text", [
    "Cookie: session_id=abcDEF123456; other=1",
    "Authorization: Bearer eyJhbGciOi.abc.def",
    '{"accessToken": "tok_123456789", "x": 1}',
    '{"refresh_token":"rt-abcdef123"}',
    '"playback_token": "pbt_secret_value"',
])
def test_redact_patterns(text):
    out = redact(text)
    assert REDACTED in out
    for secret in ("abcDEF123456", "eyJhbGciOi", "tok_123456789", "rt-abcdef123", "pbt_secret_value"):
        assert secret not in out


def test_redact_registered_secret_and_log_filter():
    register_secret("my-plain-password-xyz")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "login with %s", ("my-plain-password-xyz",), None)
    RedactingFilter().filter(record)
    assert "my-plain-password-xyz" not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_migrations_create_schema_and_are_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    latest = migrations()[-1][0]
    assert migrate(conn) == latest
    assert migrate(conn) == latest
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"sources", "episodes", "progress", "sync_runs", "sync_events", "settings"} <= tables
    seeded = conn.execute("SELECT campaign_id, collection_id FROM sources").fetchall()
    assert [tuple(r) for r in seeded] == [("14434926", "1909234")]


def test_schema_enforces_constraints(tmp_path):
    conn = connect(tmp_path / "c.db")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title, match_method) "
                     "VALUES (1, 'p1', 't', 'guess')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title) VALUES (999, 'p1', 't')")


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    return SettingsStore(conn, SecretBox(Fernet.generate_key().decode())), conn


def test_secrets_are_encrypted_at_rest(store):
    settings, conn = store
    settings.set_secret("patreon_session_id", "cookie-value-123")
    raw = conn.execute("SELECT value, is_secret FROM settings WHERE key = 'patreon_session_id'").fetchone()
    assert raw["is_secret"] == 1
    assert "cookie-value-123" not in raw["value"]
    assert settings.get_secret("patreon_session_id") == "cookie-value-123"


def test_secret_and_plain_accessors_do_not_mix(store):
    settings, _ = store
    with pytest.raises(KeyError):
        settings.get("patreon_session_id")
    with pytest.raises(KeyError):
        settings.set("pocketcasts_password", "x")
    with pytest.raises(KeyError):
        settings.set_secret("dry_run", "1")


def test_defaults(store):
    settings, _ = store
    assert settings.get_bool("dry_run") is True
    assert settings.get_int("sync_interval_minutes") == 15
