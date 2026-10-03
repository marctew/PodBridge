"""Matching rules (spec section 7)."""

from __future__ import annotations

from podbridge.matching import PatreonSide, PocketSide, match_episodes, normalise_title


def pat(i, title, published="2026-09-30T23:00:00+00:00", duration=2837.84):
    return PatreonSide(i, title, published, duration)


def pc(uuid, title, published="2026-09-30T23:02:03Z", duration=2838.0):
    return PocketSide(uuid, title, published, duration)


def test_normalise_title():
    assert normalise_title("  Hidden  Cache -\tWhere’s Pierre’s Dad? ") == "hidden cache - where's pierre's dad?"
    assert normalise_title("A – B") == normalise_title("a - b")


def test_exact_title_match():
    assert match_episodes([pat(1, "Hidden Cache - Try Not to Peep")],
                          [pc("u1", "hidden cache - try not to peep")]) == {1: ("u1", "auto_title")}


def test_duplicate_titles_use_date_and_duration_tiebreak():
    patreon = [pat(1, "Bonus", published="2026-09-01T00:00:00+00:00", duration=100)]
    pocket = [pc("u1", "Bonus", published="2026-01-01T00:00:00Z", duration=100),
              pc("u2", "Bonus", published="2026-09-01T05:00:00Z", duration=103)]
    assert match_episodes(patreon, pocket) == {1: ("u2", "auto_title")}


def test_duplicate_titles_without_unique_tiebreak_stay_unmatched():
    patreon = [pat(1, "Bonus")]
    pocket = [pc("u1", "Bonus"), pc("u2", "Bonus")]
    assert match_episodes(patreon, pocket) == {}


def test_fallback_on_date_and_duration():
    patreon = [pat(1, "Patreon title", published="2026-09-16T23:00:00+00:00", duration=1800.5)]
    pocket = [pc("u1", "Feed title", published="2026-09-17T08:00:00Z", duration=1802)]
    assert match_episodes(patreon, pocket) == {1: ("u1", "auto_duration")}


def test_fallback_rejects_outside_tolerances():
    base = pat(1, "x", published="2026-09-16T23:00:00+00:00", duration=1800)
    assert match_episodes([base], [pc("u1", "y", published="2026-09-18T00:00:00Z", duration=1800)]) == {}
    assert match_episodes([base], [pc("u1", "y", published="2026-09-17T00:00:00Z", duration=1806)]) == {}


def test_fallback_needs_complete_data():
    assert match_episodes([pat(1, "x", duration=None)], [pc("u1", "y")]) == {}
    assert match_episodes([pat(1, "x", published=None)], [pc("u1", "y")]) == {}


def test_taken_episodes_are_not_reused():
    assert match_episodes([pat(1, "Same")], [pc("u1", "Same")], taken={"u1"}) == {}


def test_contested_episode_goes_to_nobody():
    patreon = [pat(1, "First"), pat(2, "Second")]  # same date + duration, different titles
    pocket = [pc("u1", "Third")]
    assert match_episodes(patreon, pocket) == {}


def test_title_match_wins_over_fallback_competition():
    patreon = [pat(1, "Exact"), pat(2, "Other")]
    pocket = [pc("u1", "Exact")]
    # Episode 2 would also fit u1 on date + length, but u1 is already claimed by title.
    assert match_episodes(patreon, pocket) == {1: ("u1", "auto_title")}


def test_two_patreon_posts_with_same_title_get_nothing():
    patreon = [pat(1, "Same", published="2026-01-01T00:00:00+00:00"), pat(2, "Same")]
    assert match_episodes(patreon, [pc("u1", "Same")]) == {}
