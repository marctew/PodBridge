"""Builds API clients from stored credentials. Tests swap the factories via app.extensions."""

from __future__ import annotations

from pathlib import Path

from flask import current_app

from .patreon import HttpPatreonClient, PatreonClient
from .pocketcasts import HttpPocketCastsClient, PocketCastsClient, TokenCache
from .settings_store import SettingsStore
from .youtube import HttpYouTubeClient, PublicYouTubeClient, YouTubeClient


class NotConfigured(RuntimeError):
    pass


def art_dir() -> Path:
    """Artwork cache, next to the database (inside the data volume)."""
    return Path(current_app.config["PODBRIDGE"].database_path).parent / "artwork"


def _default_patreon_factory(store: SettingsStore) -> PatreonClient:
    session_id = store.get_secret("patreon_session_id")
    if not session_id:
        raise NotConfigured("Patreon session cookie is not set. Add it in Settings.")
    return HttpPatreonClient(session_id)


def patreon_client(store: SettingsStore) -> PatreonClient:
    factory = current_app.extensions.get("patreon_client_factory", _default_patreon_factory)
    return factory(store)


def pocketcasts_tokens() -> TokenCache:
    return current_app.extensions.setdefault("pocketcasts_tokens", TokenCache())


def _default_pocketcasts_factory(store: SettingsStore) -> PocketCastsClient:
    email = store.get_secret("pocketcasts_email")
    password = store.get_secret("pocketcasts_password")
    if not (email and password):
        raise NotConfigured("Pocket Casts email and password are not set. Add them in Settings.")
    return HttpPocketCastsClient(
        email, password,
        refresh_token=store.get_secret("pocketcasts_refresh_token"),
        on_refresh_token=lambda token: store.set_secret("pocketcasts_refresh_token", token),
        tokens=pocketcasts_tokens(),
    )


def pocketcasts_client(store: SettingsStore) -> PocketCastsClient:
    factory = current_app.extensions.get("pocketcasts_client_factory", _default_pocketcasts_factory)
    return factory(store)


def _default_youtube_factory(store: SettingsStore) -> YouTubeClient:
    cookies = store.get_secret("youtube_cookies")
    if not cookies:
        raise NotConfigured("YouTube cookies are not set. Add them in Settings.")
    return HttpYouTubeClient(cookies, on_cookies_changed=lambda text: store.set_secret("youtube_cookies", text))


def youtube_public_client():
    """Cookieless client for public YouTube pages (watch pages for publish dates)."""
    factory = current_app.extensions.get("youtube_public_factory", PublicYouTubeClient)
    return factory()


def youtube_client(store: SettingsStore) -> YouTubeClient:
    factory = current_app.extensions.get("youtube_client_factory", _default_youtube_factory)
    return factory(store)


def pocketcasts_configured(store: SettingsStore) -> bool:
    return store.is_set("pocketcasts_email") and store.is_set("pocketcasts_password")
