"""Pocket Casts pages: Save & test, linking a source, refresh with matching, manual match."""

from __future__ import annotations

from fakes import FakePatreonClient
from test_discovery import fixture_posts
from test_linking import fake_pocketcasts
from test_web_patreon import post, use_client

from podbridge.pocketcasts import Podcast, PocketCastsAuthError


def use_pc(app, client) -> None:
    app.extensions["pocketcasts_client_factory"] = lambda _store: client


def configure_pc(authed):
    post(authed, "/settings", page="/settings", sync_interval_minutes="15", dry_run="1",
         pocketcasts_email="me@example.com", pocketcasts_password="pw-123456")


def test_save_and_test_pocketcasts(app, authed):
    use_pc(app, fake_pocketcasts())
    html = post(authed, "/settings", page="/settings", action="test_pocketcasts", sync_interval_minutes="15",
                pocketcasts_email="me@example.com", pocketcasts_password="pw-123456").get_data(as_text=True)
    assert "Pocket Casts login works." in html
    assert authed.get("/healthz").get_json()["pocketcasts_session_valid"] is True

    use_pc(app, fake_pocketcasts(error=PocketCastsAuthError("nope")))
    html = post(authed, "/settings", page="/settings", action="test_pocketcasts",
                sync_interval_minutes="15").get_data(as_text=True)
    assert "rejected the email or password" in html
    assert authed.get("/healthz").get_json()["pocketcasts_session_valid"] is False


def test_link_page_lists_patreon_feeds_first(app, authed):
    use_pc(app, fake_pocketcasts(podcasts=[
        Podcast("a", "Alpha Show", None, "feeds.example.com"),
        Podcast("bb", "Button Boys", "GM, SK, PN", "www.patreon.com"),
    ]))
    html = authed.get("/sources/1/link").get_data(as_text=True)
    assert html.index("Button Boys") < html.index("Alpha Show")
    assert "(Patreon feed)" in html
    html = post(authed, "/sources/1/link", page="/sources", podcast_uuid="bb",
                title_bb="Button Boys").get_data(as_text=True)
    assert "Linked to Button Boys." in html
    assert "Link podcast" not in html
    assert "Button Boys ·" in html  # the linked podcast's name shows in the table


def test_refresh_without_pocketcasts_setup_warns(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    html = post(authed, "/episodes/refresh").get_data(as_text=True)
    assert "set up yet, so matching was skipped" in html


def test_refresh_matches_and_shows_pocketcasts_state(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb")
    html = post(authed, "/episodes/refresh").get_data(as_text=True)
    assert "5 episodes in feed, 3 matched (3 new), 0 unmatched" in html
    assert "20:00" in html          # Pocket Casts position 1200 s
    assert "Played" in html
    assert "Date + length" in html  # the fallback match


def test_manual_match_flow(app, authed):
    use_client(app, FakePatreonClient(fixture_posts()))
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb")
    post(authed, "/episodes/refresh")

    page = authed.get("/episodes/3/match").get_data(as_text=True)
    assert "Hidden Cache - Something Old" in page
    assert "Matched to “Hidden Cache - Try Not to Peep”" in page

    html = post(authed, "/episodes/3/match", page="/episodes/3/match", action="unlink").get_data(as_text=True)
    assert "Unlinked." in html
    html = post(authed, "/episodes/3/match", page="/episodes/3/match", uuid="pc-ep-peep").get_data(as_text=True)
    assert "Already matched" in html
    html = post(authed, "/episodes/3/match", page="/episodes/3/match", uuid="pc-ep-old").get_data(as_text=True)
    assert "Matched." in html
    assert "Manual" in html


def test_matching_returns_to_the_list_it_came_from(app, authed):
    from test_web_patreon import use_client as use_patreon
    use_patreon(app, FakePatreonClient(fixture_posts()))
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb")
    post(authed, "/episodes/refresh")
    post(authed, "/episodes/3/match", page="/episodes/3/match", action="unlink")  # make one unmatched...
    post(authed, "/episodes/3/match", page="/episodes/3/match", action="allow_auto")

    listing = authed.get("/episodes?filter=unmatched").get_data(as_text=True)
    assert "/episodes/3/match?next=/episodes?filter%3Dunmatched%23source-1" in listing.replace("&amp;", "&")
    page = authed.get("/episodes/3/match?next=/episodes?filter%3Dunmatched%23source-1")
    assert 'href="/episodes?filter=unmatched#source-1"' in page.get_data(as_text=True)
    from conftest import csrf_from
    token = csrf_from(page)
    response = authed.post("/episodes/3/match", data={"csrf_token": token, "uuid": "pc-ep-old",
                                                     "next": "/episodes?filter=unmatched#source-1"})
    assert response.headers["Location"] == "/episodes?filter=unmatched#source-1"
    offsite = authed.post("/episodes/3/match", data={"csrf_token": token, "action": "unlink",
                                                    "next": "//evil.example"})
    assert offsite.headers["Location"] == "/"


def test_continue_watching_opens_in_new_tab(app, authed):
    from podbridge.patreon import Progress
    from dataclasses import replace
    posts = [replace(p, progress=Progress(600.0, False, "is_watching", "2026-10-03T12:00:00+00:00"))
             if p.post_id == "171048709" else p for p in fixture_posts()]
    app.extensions["patreon_client_factory"] = lambda _s: FakePatreonClient(posts)
    use_pc(app, fake_pocketcasts())
    configure_pc(authed)
    post(authed, "/sources/1/link", page="/sources", podcast_uuid="pc-podcast-bb")
    post(authed, "/episodes/refresh")
    html = authed.get("/").get_data(as_text=True)
    assert 'href="/go/1" target="_blank" rel="noopener"' in html


def test_match_page_404(authed):
    assert authed.get("/episodes/999/match").status_code == 404
