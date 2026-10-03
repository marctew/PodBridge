from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from podbridge import create_app
from podbridge.config import Config

PASSWORD = "correct horse battery staple"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may make a live network call (spec section 11)."""
    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        app_password=PASSWORD,
        secret_key="x" * 48,
        encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "test.db",
        login_failure_delay=0,
    )


@pytest.fixture
def app(config):
    app = create_app(config)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match, "page has no CSRF token"
    return match.group(1)


def login(client, password: str = PASSWORD):
    token = csrf_from(client.get("/login"))
    return client.post("/login", data={"password": password, "csrf_token": token})


@pytest.fixture
def authed(client):
    response = login(client)
    assert response.status_code == 302
    return client
