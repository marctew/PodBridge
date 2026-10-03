"""How Patreon progress is interpreted for syncing."""

from __future__ import annotations

from podbridge.rules import PatreonPlayback, interpret_patreon


def test_early_watched_flag_syncs_position_not_played():
    # Observed live: Patreon flagged watched at 40:44 of 47:18.
    assert interpret_patreon(2444.0, True, 2838.0) == PatreonPlayback(False, 2444.0)


def test_within_last_minute_is_played_regardless_of_flag():
    assert interpret_patreon(2790.0, False, 2838.0) == PatreonPlayback(True, 2790.0)
    assert interpret_patreon(2838.0, True, 2838.0) == PatreonPlayback(True, 2838.0)


def test_watched_without_position_is_played():
    assert interpret_patreon(None, True, 2838.0).played is True
    assert interpret_patreon(100.0, True, None).played is True


def test_in_progress_and_not_started():
    assert interpret_patreon(381.98, False, 2838.0) == PatreonPlayback(False, 381.98)
    assert interpret_patreon(None, False, 2838.0) == PatreonPlayback(False, None)
    assert interpret_patreon(0.0, False, 2838.0) == PatreonPlayback(False, None)
