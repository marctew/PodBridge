"""Probe: does PodBridge's YouTube parsing work on your real history? READ-ONLY on YouTube.

By default it uses PodBridge's own stored cookies (run it inside the container, where the
database and ENCRYPTION_KEY are available) and saves any refreshed cookies back. Google
rotates session cookies and invalidates the old values, so two clients using separate
copies of one session knock each other out; sharing PodBridge's copy avoids that.

Set YOUTUBE_COOKIES_FILE to use a cookies.txt export instead. Refreshed cookies are then
NOT saved anywhere, so only do that with a session PodBridge isn't using.

Prints titles, percentages and parse results, never cookie values.

Usage (inside the container):
  docker compose exec podbridge python scripts/probe_youtube.py @TheNewsAgents
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podbridge.youtube import HttpYouTubeClient  # noqa: E402


def make_client() -> HttpYouTubeClient:
    path = os.environ.get("YOUTUBE_COOKIES_FILE")
    if path:
        print(f"Using cookies from {path} (refreshed cookies will NOT be saved)")
        return HttpYouTubeClient(Path(path).read_text(encoding="utf-8"), on_cookies_changed=lambda _t: None)

    from podbridge.config import Config
    from podbridge.crypto import SecretBox
    from podbridge.db import connect
    from podbridge.settings_store import SettingsStore

    config = Config.from_env()
    store = SettingsStore(connect(config.database_path), SecretBox(config.encryption_key))
    cookies = store.get_secret("youtube_cookies")
    if not cookies:
        sys.exit("No YouTube cookies stored in PodBridge. Add them in Settings, or set YOUTUBE_COOKIES_FILE.")
    print("Using PodBridge's stored cookies (refreshed cookies are saved back)")
    return HttpYouTubeClient(cookies, on_cookies_changed=lambda text: store.set_secret("youtube_cookies", text))


def main() -> None:
    client = make_client()

    print("\n=== Session ===")
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
            uploads = client.channel_videos(channel.channel_id)
            print(f"  uploads listed: {len(uploads)}, with a length: {sum(1 for u in uploads if u.duration_secs)}")
            for upload in uploads[:5]:
                print(f"    {upload.duration_secs or 0:7.0f}s  {upload.title[:70]}")

    print("\nDone (read-only). Paste this output back to Claude.")


if __name__ == "__main__":
    main()
