"""Sources that cover every post in a campaign, not just one collection."""

from __future__ import annotations

from fakes import FakePatreonClient, FakeResponse
from test_discovery import fixture_posts
from test_linking import env, fake_pocketcasts, matches  # noqa: F401  (env is a fixture)
from test_patreon import client_with
from test_web_patreon import post, use_client

from podbridge.discovery import discover_all
from podbridge.linking import MatchError, link_source, refresh_all, set_manual_match, widen_source
from podbridge.patreon import Post, Progress


def test_list_posts_without_collection_uses_campaign_feed_filters():
    client, session = client_with(FakeResponse(200, {"data": []}))
    client.list_posts("14434926", None)
    params = session.calls[0]["params"]
    assert "filter[collection_id]" not in params
    assert params["filter[campaign_id]"] == "14434926"
    assert params["sort"] == "-published_at"
    assert params["filter[contains_exclusive_posts]"] == "true"


def test_widen_keeps_matches_and_adds_new_posts(env):  # noqa: F811
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    widen_source(conn, 1)
    source = conn.execute("SELECT label, collection_id FROM sources WHERE id = 1").fetchone()
    assert tuple(source) == ("Button Boys: all posts", "")

    extra = Post("170000099", "Non-Fatal Flaws: Bad Bits in Good Games (Ad-Free)", "2026-09-30T23:00:00+00:00",
                 None, "podcast", "755000099", 3437.0, Progress(None, False, "is_not_watched", None))
    patreon = FakePatreonClient(fixture_posts() + [extra])
    discover_all(conn, store, patreon)
    assert patreon.list_calls[-1] == ("14434926", None)
    [result] = refresh_all(conn, store, fake_pocketcasts())
    assert result.newly_matched == 1
    found = matches(conn)
    assert found["170000099"][:2] == ("pc-ep-nff", "auto_title")
    assert found["171048709"][:2] == ("pc-ep-peep", "auto_title")  # kept


def test_widen_refuses_a_second_all_posts_source(env):  # noqa: F811
    conn, _ = env
    with conn:
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id) VALUES ('x', '14434926', '')")
    try:
        widen_source(conn, 1)
    except MatchError as exc:
        assert "already has an all-posts source" in str(exc)
    else:
        raise AssertionError("expected MatchError")


def test_disabled_source_matches_do_not_block(env):  # noqa: F811
    conn, store = env
    refresh_all(conn, store, fake_pocketcasts())
    with conn:
        conn.execute("UPDATE sources SET enabled = 0 WHERE id = 1")
        conn.execute("INSERT INTO sources (label, campaign_id, collection_id) VALUES ('All', '14434926', '')")
    link_source(conn, 2, "pc-podcast-bb")
    discover_all(conn, store, FakePatreonClient(fixture_posts()))
    [result] = refresh_all(conn, store, fake_pocketcasts())
    assert result.source_id == 2 and result.newly_matched == 3
    new_ep = conn.execute("SELECT id FROM episodes WHERE source_id = 2 AND patreon_post_id = '170000003'").fetchone()
    set_manual_match(conn, new_ep[0], "pc-ep-old")  # not blocked by the disabled source either


def test_add_all_posts_source_and_widen_via_ui(app, authed):
    from podbridge.patreon import Collection
    use_client(app, FakePatreonClient(collections=[Collection("1968959", "Player 4", 7)]))
    html = authed.get("/sources?campaign_id=999").get_data(as_text=True)
    assert "All posts in the campaign (recommended)" in html
    html = post(authed, "/sources", page="/sources", campaign_id="999", collection_id="all",
                default_label="All posts").get_data(as_text=True)
    assert "Added All posts." in html
    html = post(authed, "/sources/1/widen", page="/sources").get_data(as_text=True)
    assert "Now covers every post in the campaign" in html
    assert "Button Boys: all posts" in html
