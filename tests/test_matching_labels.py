"""Label prefixes ('Q&A:') and matching before a YouTube video's date is known (The Crime Agents)."""

from __future__ import annotations

from podbridge.matching import PatreonSide, PocketSide, match_episodes_loose, strip_label, title_similarity

# From the real Crime Agents feed: YouTube drops the "Q&A:" prefix and rewords the title.
CRIME_AGENTS_PC = [
    PocketSide("putney", "Q&A: Why is the 'Putney Pusher' case still unsolved?", "2026-09-24T05:00:00Z", 1774),
    PocketSide("suffragettes", "Q&A: would the Suffragettes be labelled 'terrorists' under today's laws?",
               "2026-09-17T05:00:00Z", 1479),
    PocketSide("double", "Cracking an 'unsolvable' double murder", "2026-09-14T05:00:00Z", 2999),
    PocketSide("dover", "Q&A: Were police too soft on masked mobs in Dover & Portsmouth?",
               "2026-09-10T05:00:00Z", 1341),
    PocketSide("hillsborough", "Hillsborough: the cover-up that shamed British policing", "2026-09-07T05:00:00Z", 3536),
    PocketSide("kinahan", "Q&A: Kinahan in court & how police let Simon Levy kill", "2026-08-13T05:00:00Z", 1705),
    PocketSide("harper", "Q&A: PC Andrew Harper's killers, funeral director horror & why cops don't report "
                         "their own", "2026-08-06T05:00:00Z", 1753),
    PocketSide("prison", "Q&A: Prison early release scheme, domestic abuse & how did Russia find the Skripals?",
               "2026-07-30T05:00:00Z", 1819),
]


def test_strip_label():
    assert strip_label("Q&A: Why is the case unsolved?") == "Why is the case unsolved?"
    assert strip_label("Special episode: why Counter-Terror Police took over") == "why Counter-Terror Police took over"
    assert strip_label("No label here") == "No label here"
    assert strip_label("A very long run of words before: the colon") == "A very long run of words before: the colon"


def test_prefix_on_one_side_still_matches_exactly():
    video = [PatreonSide(1, "Cracking an 'unsolvable' double murder | The Crime Agents", "2026-09-14T12:00:00Z", 2990)]
    pocket = [PocketSide("x", "Q&A: Cracking an 'unsolvable' double murder", "2026-01-01T00:00:00Z", 2999)]
    assert match_episodes_loose(video, pocket) == {1: ("x", "auto_title")}


def test_hillsborough_label_is_not_lost():
    # "Hillsborough:" is part of the title, not a label; comparing every variant keeps it.
    assert title_similarity("Hillsborough: the cover-up that shamed British policing",
                            "Hillsborough: the cover-up that shamed British policing") == 1.0


def test_putney_pusher_matches_before_the_date_is_known():
    video = [PatreonSide(1, "The Putney Pusher: will the case ever be solved? | The Crime Agents", None, 1765)]
    assert match_episodes_loose(video, CRIME_AGENTS_PC) == {1: ("putney", "auto_date")}


def test_no_date_fallback_refuses_when_nothing_clearly_wins():
    video = [PatreonSide(1, "Police questions answered | The Crime Agents", None, 1750)]
    assert match_episodes_loose(video, CRIME_AGENTS_PC) == {}


def test_no_date_fallback_respects_length():
    video = [PatreonSide(1, "The Putney Pusher: will the case ever be solved?", None, 3600)]  # twice as long
    assert match_episodes_loose(video, CRIME_AGENTS_PC) == {}
