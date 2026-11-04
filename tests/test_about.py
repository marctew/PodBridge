"""The About page and the sidebar footer's version line (Design Uniformity 7.15)."""
from podbridge import __version__


def test_about_needs_a_login(client):
    assert client.get("/about").status_code == 302


def test_about_shows_the_version_and_counts(authed):
    html = authed.get("/about").get_data(as_text=True)
    assert f"v{__version__}" in html
    assert "Your data" in html and "Built with" in html and "Sync runs" in html


def test_every_page_ends_with_the_version_line(authed):
    html = authed.get("/").get_data(as_text=True)
    assert 'class="version-link"' in html and "· About" in html and "Sign out" in html
