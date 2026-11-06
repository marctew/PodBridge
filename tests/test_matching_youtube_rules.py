"""YouTube matching: length sanity, strong titles across a wider window, containment, Shorts."""

from __future__ import annotations

from test_youtube import CHANNEL, FakeYouTube, yt_env  # noqa: F401  (yt_env is a fixture)

from podbridge.discovery import discover_youtube_all
from podbridge.linking import revalidate_fuzzy_matches
from podbridge.matching import (
    PatreonSide, PocketSide, fuzzy_match_holds, lengths_plausible, match_episodes_loose, title_similarity,
)
from podbridge.youtube import PlaylistItem

DAY = "2026-10-16T12:00:00Z"


def video(title, published=DAY, secs=3000.0, ep_id=1):
    return PatreonSide(ep_id, title, published, secs)


def pod(uuid, title, published=DAY, secs=3000.0):
    return PocketSide(uuid, title, published, secs)


def test_a_clip_is_not_matched_to_the_only_episode_that_day():
    clip = video("Max was there for me", secs=42.0)
    assert match_episodes_loose([clip], [pod("p", "The Mindset Shift That Won Ash Barty Wimbledon", secs=3226)]) == {}
    nine_min = video("Sergio Perez: How Red Bull Reacted When I Beat Verstappen", secs=551.0)
    assert match_episodes_loose([nine_min], [pod("p", "Fernando Alonso: The Real Pain of Losing", secs=3219)]) == {}


def test_the_only_episode_that_day_still_matches_on_length_or_title():
    retitled = video("Russell Brand the rapist | The News Agents", secs=2280.0)
    assert match_episodes_loose([retitled], [pod("p", "Who will Trump blame?", secs=2500)]) == {1: ("p", "auto_date")}
    longer = video("Lewis Moody on choosing to live", secs=3000.0)  # ads make the podcast 40% longer
    assert match_episodes_loose([longer], [pod("p", "Lewis Moody: Choosing To Live", secs=4200)]) == {
        1: ("p", "auto_date")}


def test_a_rival_ruled_out_by_length_still_blocks_a_weak_title():
    ep = video("Did Jesus Exist? The Truth Historians Can't Ignore", secs=1440.0)
    pocket = [pod("deepwater", "ShortHand: The Deepwater Horizon Oil Spill", secs=1360),
              pod("long", "Episode 400: Something else entirely", secs=5400)]
    assert match_episodes_loose([ep], pocket) == {}


def test_strong_title_matches_across_days():
    ep = video("Lewis Moody On Preparing His Family for The End & Choosing To Live", "2026-10-28T12:00:00Z", 4920)
    pocket = [pod("moody", "Lewis Moody: Preparing His Family for The End & Choosing To Live (E446)",
                  "2026-10-24T07:00:00Z", 4860),
              pod("other", "Why The Best Never Stop Feeling Fear", "2026-10-28T05:00:00Z", 3600)]
    assert match_episodes_loose([ep], pocket) == {1: ("moody", "auto_date")}
    too_old = [pod("moody", "Lewis Moody: Preparing His Family for The End & Choosing To Live (E446)",
                   "2026-09-01T07:00:00Z", 4860)]
    assert match_episodes_loose([ep], too_old) == {}


def test_strong_title_needs_a_clear_winner():
    ep = video("The Case of O.J. Simpson PART 1: The Juice", "2026-10-20T12:00:00Z")
    pocket = [pod("a", "The Case of O.J. Simpson Part 1: The Juice is Loose", "2026-10-15T12:00:00Z"),
              pod("b", "The Case of O.J. Simpson Part 2: The Juice is Loose", "2026-10-16T12:00:00Z")]
    assert match_episodes_loose([ep], pocket) == {}


def test_containment_scores_short_titles_inside_long_ones():
    assert title_similarity("South African Actor Greg Kriek",
                            "South African Actor Greg Kriek Moving to LA, Acting Alongside Legends") >= 0.8
    assert title_similarity("Greg Kriek interview", "Greg Kriek Moving to LA, Acting Alongside Legends") < 0.8


def test_fuzzy_match_holds():
    assert lengths_plausible(None, 3000) and not lengths_plausible(60, 3000)
    assert not fuzzy_match_holds("Clip", DAY, 60, "Full episode", DAY, 3000)
    assert fuzzy_match_holds("Different words", DAY, 3000, "Other title", DAY, 3100)
    assert fuzzy_match_holds("Jimmy Carr: Stop Overthinking Your Life & Fix Your Anxious Mind", "2026-10-25T12:00:00Z",
                             5220, "Jimmy Carr: How To Fix Your Mind, Stop Overthinking & Take Control (E445)",
                             "2026-10-23T13:00:00Z", 5160)
    assert not fuzzy_match_holds("Something", "2026-10-25T12:00:00Z", 3000, "Else", "2026-10-21T12:00:00Z", 3000)


def test_revalidation_drops_clip_matches_only(yt_env):  # noqa: F811
    conn, _ = yt_env
    source_id = conn.execute("SELECT id FROM sources WHERE campaign_id = ?", (CHANNEL,)).fetchone()[0]
    with conn:
        for uuid, title, secs in [("p-clip", "Full episode", 3000), ("p-ok", "Another full episode", 3000),
                                  ("p-manual", "Manual one", 3000)]:
            conn.execute("INSERT INTO pocketcasts_episodes (uuid, podcast_uuid, title, published_at, duration_secs, fetched_at) "
                         "VALUES (?, 'pc-news', ?, ?, ?, ?)", (uuid, title, DAY, secs, DAY))
        for post, secs, uuid, method in [("Clip0000001", 60, "p-clip", "auto_date"),
                                         ("Episode0001", 2900, "p-ok", "auto_date"),
                                         ("Manual00001", 60, "p-manual", "manual")]:
            conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title, published_at, duration_secs, "
                         "pocketcasts_episode_uuid, match_method) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (source_id, post, f"Video {post}", DAY, secs, uuid, method))
    assert revalidate_fuzzy_matches(conn, source_id) == 1
    kept = dict(conn.execute("SELECT patreon_post_id, match_method FROM episodes").fetchall())
    assert kept == {"Clip0000001": "none", "Episode0001": "auto_date", "Manual00001": "manual"}


def test_shorts_are_not_listed_and_unmatched_ones_are_removed(yt_env):  # noqa: F811
    conn, store = yt_env
    source_id = conn.execute("SELECT id FROM sources WHERE campaign_id = ?", (CHANNEL,)).fetchone()[0]
    with conn:  # listed before its length was known, then found to be a Short
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title, duration_secs) "
                     "VALUES (?, 'OldShort001', 'Old short', 45)", (source_id,))
        conn.execute("INSERT INTO episodes (source_id, patreon_post_id, title, duration_secs, match_method, "
                     "pocketcasts_episode_uuid) VALUES (?, 'Trailer0001', 'Trailer', 60, 'auto_title', 'p-t')",
                     (source_id,))
    uploads = {CHANNEL: [PlaylistItem("ShortClip01", "Clip", 50.0), PlaylistItem("FullEpisod1", "Episode", 2400.0),
                         PlaylistItem("NoLength001", "Unknown length", None)]}
    [result] = discover_youtube_all(conn, store, FakeYouTube([], {}, uploads=uploads))
    ids = {r[0] for r in conn.execute("SELECT patreon_post_id FROM episodes")}
    assert ids == {"FullEpisod1", "NoLength001", "Trailer0001"}
    assert result.added == 2
