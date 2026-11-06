"""Home screen icons and the manifest (Design Uniformity PWA-ICONS.md)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

OLD = {  # URLs phones and pages knew before PWA-ICONS.md, and where they go now
    "/favicon.ico": "/static/favicon.ico",
    "/static/manifest.webmanifest": "/static/manifest.json",
    "/static/icons/icon.svg": "/static/img/icon.svg",
    "/static/icons/favicon-32.png": "/static/favicon.ico",
    "/static/icons/apple-touch-icon.png": "/static/apple-touch-icon.png",
    "/static/icons/icon-192.png": "/static/icon-192.png",
    "/static/icons/icon-512.png": "/static/icon-512.png",
    "/static/icons/icon-maskable-512.png": "/static/icon-512.png",
}


def test_icons_load_without_signing_in(client):
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.mimetype == "image/png", name
        img = Image.open(io.BytesIO(r.data))
        assert img.size == (size, size) and img.mode == "RGB", name      # opaque: no black corners on iOS
    ico = client.get("/static/favicon.ico")
    assert ico.status_code == 200 and ico.mimetype.startswith("image/")
    assert Image.open(io.BytesIO(ico.data)).size == (32, 32)
    svg = client.get("/static/img/icon.svg")
    assert svg.status_code == 200 and svg.mimetype == "image/svg+xml"


def test_logo_has_a_square_tile_first():
    logo = (Path(__file__).parent.parent / "podbridge" / "static" / "brand" / "logo.svg").read_text()
    body = logo[logo.index("<svg"):]
    assert body.split("\n")[1].strip().startswith('<rect width="64" height="64" rx="0"')


def test_manifest(client):
    r = client.get("/static/manifest.json")
    assert r.status_code == 200 and r.mimetype in ("application/json", "application/manifest+json")
    m = json.loads(r.data)
    assert m["name"] == m["short_name"] == "PodBridge" and m["display"] == "standalone"
    assert m["background_color"] == m["theme_color"] == "#090b11"
    assert {i["src"] for i in m["icons"]} == {"/static/icon-192.png", "/static/icon-512.png"}
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for i in m["icons"]:
        assert client.get(i["src"]).status_code == 200


def test_old_icon_urls_redirect_without_signing_in(client):
    for old, new in OLD.items():
        r = client.get(old)
        assert r.status_code == 301 and r.headers["Location"].endswith(new), old
        assert client.get(r.headers["Location"]).status_code == 200, old
    assert client.get("/static/icons/nothing.png").status_code == 404


def test_head_tags_on_the_app_and_the_login_page(client, authed):
    from conftest import csrf_from
    home = authed.get("/")
    pages = [home.get_data(as_text=True)]
    authed.post("/logout", data={"csrf_token": csrf_from(home)})
    pages.append(client.get("/login").get_data(as_text=True))
    assert "Log in" in pages[1]
    for html in pages:
        assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in html
        assert '<meta name="apple-mobile-web-app-title" content="PodBridge">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html
        assert 'rel="manifest" href="/static/manifest.json?v=' in html
        assert 'href="/static/img/icon.svg"' in html and 'href="/static/favicon.ico" sizes="32x32"' in html
        assert "static/icons/" not in html and "webmanifest" not in html
        assert html.count('<meta name="theme-color"') == 1                  # only the one from theme_head.html

