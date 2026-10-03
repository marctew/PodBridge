"""Builds API clients from stored credentials. Tests swap the factories via app.extensions."""

from __future__ import annotations

from flask import current_app

from .patreon import HttpPatreonClient, PatreonClient
from .settings_store import SettingsStore


class NotConfigured(RuntimeError):
    pass


def _default_patreon_factory(store: SettingsStore) -> PatreonClient:
    session_id = store.get_secret("patreon_session_id")
    if not session_id:
        raise NotConfigured("Patreon session cookie is not set. Add it in Settings.")
    return HttpPatreonClient(session_id)


def patreon_client(store: SettingsStore) -> PatreonClient:
    factory = current_app.extensions.get("patreon_client_factory", _default_patreon_factory)
    return factory(store)
