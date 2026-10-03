"""Probe: does PodBridge's YouTube parsing work on your real history? READ-ONLY.

Uses the same client the app uses. Reads cookies from the file in
YOUTUBE_COOKIES_FILE (a cookies.txt export). Prints titles, percentages and
parse results, never cookie values. Refreshed cookies are NOT saved back.

Usage:  YOUTUBE_COOKIES_FILE=/root/yt-cookie-check/cookies.txt python3 scripts/probe_youtube.py [@channel]
Needs the app's dependencies (run it inside the container, or pip install -r requirements.txt).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podbridge.youtube import HttpYouTubeClient  # noqa: E402


def main() -> None:
    path = os.environ.get("YOUTUBE_COOKIES_FILE")
    if not path:
        sys.exit("Set YOUTUBE_COOKIES_FILE to a cookies.txt export.")
    client = HttpYouTubeClient(Path(path).read_text(encoding="utf-8"), on_cookies_changed=lambda _t: None)

    print("=== Session ===")
    logged_in = client.check_session()
    print(f"  signed in: {logged_in}")
    if not logged_in:
        return

    print("\n=== History (first page) ===")
    items = client.history()
    with_bar = [i for i in items if i.percent is not None]
    print(f"  items: {len(items)}, with a progress bar: {len(with_bar)}")
    for item in with_bar[:8]:
        print(f"    {item.percent:5.1f}%  {item.title[:70]}")

    print("\n=== Watch page details (first 3 with progress) ===")
    for item in with_bar[:3]:
        d = client.video_details(item.video_id)
        if d is None:
            print(f"  {item.video_id}: could not parse")
            continue
        print(f"  {d.title[:60]!r}: channel={d.channel_id} published={d.published_at} length={d.duration_secs}")

    if len(sys.argv) > 1:
        print("\n=== Channel ===")
        channel = client.resolve_channel(sys.argv[1])
        print(f"  {sys.argv[1]} -> {channel}")
        if channel:
            checked = with_bar[:10]
            ours = [i for i in checked
                    if (d := client.video_details(i.video_id)) and d.channel_id == channel.channel_id]
            print(f"  of the {len(checked)} most recent watches with progress, {len(ours)} are from this channel")

    print("\nDone (read-only). Paste this output back to Claude.")


if __name__ == "__main__":
    main()
