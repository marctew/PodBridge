"""Static files and caching (APP-STANDARD 12): pages are no-store, static files no-cache, and every static link
carries ?v=<mtime>, so a deploy's CSS and JS show at once, through the tunnel as well as on the LAN."""

from __future__ import annotations

import re

from podbridge import __version__


def _static_links(html):
    return re.findall(r'(?:href|src)="(/static/[^"]*)"', html)


def test_static_links_carry_a_version(authed):
    html = authed.get("/").get_data(as_text=True)
    links = _static_links(html)
    assert links, "the page links no static files"
    for link in links:
        # The home screen icon and manifest keep their own v=app_version (PWA-ICONS.md 2).
        if link.startswith(("/static/apple-touch-icon.png", "/static/manifest.json")):
            assert link.endswith(f"?v={__version__}"), link
        else:
            assert re.search(r"\?v=\d+$", link), link


def test_pages_are_no_store(app, authed):
    for r in (authed.get("/"), app.test_client().get("/login")):
        assert r.status_code == 200 and r.mimetype == "text/html"
        assert r.headers["Cache-Control"] == "no-store"


def test_static_files_are_no_cache(client):
    for path in ("/static/style.css", "/static/design/uniform.css"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "no-cache"


def test_bottom_nav_more_is_the_shared_markup(authed):
    html = authed.get("/settings").get_data(as_text=True)
    assert '<details class="menu-wrap nav-more">' in html
    assert "more-menu" not in html
    more = html[html.index('class="menu-wrap nav-more"'):]
    assert '<summary class="active">' in more                     # Settings is inside More
    assert 'document.querySelector(".nav-more")' in html          # closes on a tap outside, or Escape
