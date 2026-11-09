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


def _every_plain_get(app):
    """Every GET route without a URL parameter, except event streams that never end and the sign-out."""
    for rule in app.url_map.iter_rules():
        if ("GET" in rule.methods and not rule.arguments and rule.endpoint != "static"
                and "events" not in rule.rule and "logout" not in rule.rule):
            yield rule.rule


def test_nothing_is_left_for_a_shared_cache(app, authed):
    """APP-STANDARD 12 (Encore, 9 Nov 2026): every answer that isn't a static file is no-store or private, signed in
    or not, so Cloudflare never keeps one and hands it to someone else. Unmarked JSON is what it cached."""
    anon = app.test_client()
    for path in _every_plain_get(app):
        for who, client in (("signed in", authed), ("signed out", anon)):
            r = client.get(path)
            cc = r.headers.get("Cache-Control", "")
            assert ("no-store" in cc or "private" in cc) and "public" not in cc, f"{path} ({who}, {r.status_code}): {cc!r}"


def test_signed_in_json_is_no_store(authed):
    r = authed.get("/api/v1/meta")
    assert r.status_code == 200 and r.mimetype == "application/json", (r.status_code, r.mimetype)
    assert r.headers["Cache-Control"] == "no-store"


def test_public_is_made_private(app):
    """send_file says public when given a max_age; a shared cache would keep that and serve it to anyone."""
    from flask import Response

    with app.test_request_context("/somewhere"):
        resp = Response(b"x", mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=60"
        assert app.process_response(resp).headers["Cache-Control"] == "private, max-age=60"
