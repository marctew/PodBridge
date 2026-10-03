"""Plex-style Library, show pages and the artwork cache."""

from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_linking import fake_pocketcasts
from test_web_patreon import post
from test_web_pocketcasts import configure_pc, use_pc

from podbridge import artwork
from podbridge.crypto import SecretBox
from podbridge.db import connect, migrate
from podbridge.discovery import discover_all
from podbridge.patreon import Progress, parse_post
from podbridge.settings_store import SettingsStore

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


class FakeImageResponse:
    def __init__(self, status=200, content_type="image/jpeg", body=JPEG):
        self.status_code, self.headers, self._body = status, {"Content-Type": content_type}, body

    def iter_content(self, _size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeImageSession:
    def __init__(self, *responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


# --- artwork cache ---

@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "a.db")
    migrate(c)
    return c


def test_ensure_downloads_once_and_caches(conn, tmp_path):
    session = FakeImageSession(FakeImageResponse())
    path = artwork.ensure(conn, tmp_path / "art", "yt:NewsAgent01", session=session)
    assert path.read_bytes() == JPEG and path.suffix == ".jpg"
    assert session.urls == ["https://i.ytimg.com/vi/NewsAgent01/mqdefault.jpg"]
    assert artwork.ensure(conn, tmp_path / "art", "yt:NewsAgent01", session=FakeImageSession()) == path


def test_ensure_rejects_non_images_and_remembers_failure(conn, tmp_path):
    session = FakeImageSession(FakeImageResponse(content_type="text/html"), FakeImageResponse(status=404),
                               FakeImageResponse(body=b"x" * (artwork.MAX_BYTES + 1)))
    assert artwork.ensure(conn, tmp_path / "art", "podcast:abc", session=session) is None
    assert len(session.urls) == 3  # tried every podcast-art URL
    assert artwork.ensure(conn, tmp_path / "art", "podcast:abc", session=FakeImageSession()) is None  # not retried yet


def test_patreon_thumbnail_parsing_prefers_creator_thumbnail():
    item = {"id": "1", "type": "post", "attributes": {
        "title": "t", "thumbnail": {"large": "https://c10.patreonusercontent.com/large.jpg?token=SIGNED"},
        "post_file": {"media_id": 5, "default_thumbnail": {"url": "https://frame.jpg"}}}}
    post_ = parse_post(item)
    assert post_.thumbnail_url.endswith("large.jpg?token=SIGNED")
    assert "SIGNED" not in repr(post_)  # signed URL kept out of reprs/logs
    item["attributes"].pop("thumbnail")
    assert parse_post(item).thumbnail_url == "https://frame.jpg"


def test_discovery_downloads_patreon_thumbnails_within_budget(conn, tmp_path, monkeypatch):
    store = SettingsStore(conn, SecretBox(Fernet.generate_key().decode()))
    posts = [replace(p, thumbnail_url=f"https://thumbs.example/{p.post_id}.jpg") for p in fixture_posts()]
    fetched: list[str] = []
    monkeypatch.setattr(artwork, "download", lambda url, session=None: fetched.append(url) or (JPEG, "image/jpeg"))
    monkeypatch.setattr(artwork, "MAX_PATREON_DOWNLOADS_PER_RUN", 2)
    discover_all(conn, store, FakePatreonClient(posts), tmp_path / "art")
    assert len(fetched) == 2
    discover_all(conn, store, FakePatreonClient(posts), tmp_path / "art")
    assert len(fetched) == 3  # the third media post; cached ones aren't refetched
    assert artwork.has_art(conn, "patreon:171048709")


# --- pages ---

def setup_library(app, authed, peep=Progress(600.0, False, "is_watching", "2026-10-03T12:00:00+00:00")):
    posts = [replace(p, progress=peep) if p.post_id == "171048709" else p for p in fixture_posts()]
    app.extensions["patreon_client_factory"] = lambda _s: FakePatreonClient(posts)
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb", title_bb="x",
         **{"title_pc-podcast-bb": "Button Boys"})
    post(authed, "/episodes/refresh")


def test_library_and_show_page(app, authed):
    setup_library(app, authed)
    library = authed.get("/library").get_data(as_text=True)
    assert "Button Boys" in library and "/library/1/pc-podcast-bb" in library
    assert "/art/podcast:pc-podcast-bb" in library

    show = authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)
    assert "Hidden Cache - Try Not to Peep" in show
    assert "/art/patreon:171048709" in show            # episode thumbnail
    assert "▶ Continue" in show and "20:00" in show    # Pocket Casts is ahead (1200 s)
    assert 'class="played"' in show                     # Pierre's Dad: played
    assert 'class="corner"' in show                     # audio post: not started

    in_progress = authed.get("/library/1/pc-podcast-bb?filter=in_progress").get_data(as_text=True)
    assert "Try Not to Peep" in in_progress and "Pierre" not in in_progress
    assert authed.get("/library/1/nope").status_code == 404


def test_pocketcasts_links():
    from podbridge.library import pocketcasts_links
    assert pocketcasts_links("pod-1", "ep-1", 1200.7) == (
        "https://pocketcasts.com/podcasts/pod-1/ep-1", "https://pca.st/episode/ep-1?t=1200")
    assert pocketcasts_links("pod-1", "ep-1", None)[1] == "https://pca.st/episode/ep-1"
    assert pocketcasts_links(None, "ep-1", 5) == (None, None)


def test_open_in_pocketcasts_links_on_pages(app, authed):
    setup_library(app, authed)
    show = authed.get("/library/1/pc-podcast-bb").get_data(as_text=True)
    assert 'href="https://pocketcasts.com/podcasts/pc-podcast-bb/pc-ep-peep"' in show
    assert 'data-direct="https://pca.st/episode/pc-ep-peep?t=1200"' in show  # Pocket Casts is ahead at 20:00
    matching = authed.get("/episodes").get_data(as_text=True)
    assert "https://pocketcasts.com/podcasts/pc-podcast-bb/pc-ep-dad" in matching


def test_home_shelves(app, authed):
    setup_library(app, authed)
    home = authed.get("/").get_data(as_text=True)
    assert "Continue watching" in home and "Recently added" in home
    assert "20:00 of 47:17 · Button Boys" in home


def test_art_route_validates_keys_and_serves_cache(app, authed, monkeypatch):
    assert authed.get("/art/../../etc/passwd").status_code == 404
    assert authed.get("/art/unknown:abc").status_code == 404
    monkeypatch.setattr(artwork, "download", lambda url, session=None: (JPEG, "image/jpeg"))
    response = authed.get("/art/yt:NewsAgent01")
    assert response.status_code == 200 and response.data == JPEG
    assert response.mimetype == "image/jpeg"
    assert authed.get("/art/patreon:123").status_code == 404  # never fetched on demand


def test_art_requires_login(client):
    assert client.get("/art/yt:NewsAgent01").status_code == 302
