"""Stats page: time accounting, streaks, weekly buckets."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from podbridge import stats
from podbridge.db import connect, migrate

TZ = "Europe/London"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    migrate(c)
    with c:
        for i, (title, duration) in enumerate((("A", 3000.0), ("B", 2000.0)), start=1):
            c.execute("INSERT INTO episodes (id, source_id, patreon_post_id, title, duration_secs) VALUES (?, 1, ?, ?, ?)",
                      (i, f"p{i}", title, duration))
    return c


def log(conn, episode_id, side, position, at, played=0):
    with conn:
        conn.execute("INSERT INTO watch_history (episode_id, side, position_secs, played, at) VALUES (?, ?, ?, ?, ?)",
                     (episode_id, side, position, played, at))


def test_furthest_position_counts_once_across_apps(conn):
    log(conn, 1, "patreon", 2400, "2026-10-01T10:00:00Z")      # 40:00 watched on Patreon
    log(conn, 1, "pocketcasts", 2700, "2026-10-01T18:00:00Z")  # then 40:00 -> 45:00 in Pocket Casts
    log(conn, 1, "patreon", 600, "2026-10-02T10:00:00Z")       # rewatching the start adds nothing
    segs = stats.segments(conn, TZ)
    assert [(s.side, s.secs) for s in segs] == [("patreon", 2400), ("pocketcasts", 300), ("patreon", 0)]


def test_played_counts_to_the_end(conn):
    log(conn, 2, "youtube", 1000, "2026-10-01T10:00:00Z")
    log(conn, 2, "pocketcasts", None, "2026-10-01T11:00:00Z", played=1)
    assert [s.secs for s in stats.segments(conn, TZ)] == [1000, 1000]


def test_huge_jumps_are_capped(conn):
    with conn:
        conn.execute("UPDATE episodes SET duration_secs = 50000 WHERE id = 1")
    log(conn, 1, "patreon", 40000, "2026-10-01T10:00:00Z")
    assert stats.segments(conn, TZ)[0].secs == stats.MAX_JUMP_SECS


def test_streaks():
    today = date(2026, 10, 4)
    days = {today - timedelta(days=n) for n in (0, 1, 2, 5, 6, 7, 8)}
    assert stats._streaks(days, today) == (3, 4)
    assert stats._streaks({today - timedelta(days=1)}, today) == (1, 1)   # yesterday still counts
    assert stats._streaks({today - timedelta(days=3)}, today) == (0, 1)


def test_nice_ceiling():
    assert [stats._nice_ceiling(v) for v in (0, 0.4, 3.4, 7, 12, 26)] == [1, 1, 5, 10, 20, 50]


def test_summary_buckets_by_week_and_app(conn):
    now = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)   # a Sunday
    log(conn, 1, "patreon", 1800, "2026-10-03T10:00:00Z")      # this week
    log(conn, 2, "youtube", 600, "2026-09-24T10:00:00Z")       # last week
    log(conn, 2, "pocketcasts", None, "2026-09-25T10:00:00Z", played=1)
    s = stats.summarize(conn, TZ, {1: "Show A", 2: "Show B"}, weeks=4, now=now)
    assert s["this_week"] == 1800
    assert s["weekly"][-1]["by_side"]["patreon"] == 1800 and s["weekly"][-1]["current"]
    assert s["weekly"][-2]["by_side"] == {"patreon": 0, "youtube": 600, "pocketcasts": 1400}
    assert s["finished_all"] == 1 and s["completion"] == 0.5
    assert [r["title"] for r in s["shows"]] == ["Show B", "Show A"]
    assert s["axis_max"] >= max(w["total"] for w in s["weekly"])


def test_stats_page(app, authed):
    from test_library import setup_library
    setup_library(app, authed)
    html = authed.get("/stats").get_data(as_text=True)
    assert "Time per week" in html and "Show as a table" in html
    assert "Pocket Casts" in html and "By show, last 30 days" in html
    assert 'href="/stats"' in authed.get("/history").get_data(as_text=True)


def test_stats_page_empty(authed):
    assert "No listening recorded yet" in authed.get("/stats").get_data(as_text=True)
