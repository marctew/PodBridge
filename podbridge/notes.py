"""Show notes for episode pages: fetched on first view, cached, shown as plain text.

Notes arrive as HTML from Patreon and Pocket Casts. They're converted to plain text (never
rendered as HTML, so nothing in a post can inject markup), then two kinds of link are added
back: web links, and timestamps like 12:30 or 1:02:45, which open the episode at that moment.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from html import escape
from html.parser import HTMLParser

from markupsafe import Markup

from .db import utcnow
from .resume import build_resume_url

log = logging.getLogger(__name__)

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}
URL = re.compile(r"https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]")
# 12:30 or 1:02:45, but not a clock time like 10:30am / 10:30 p.m.
TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])(?!\s?[ap]\.?m\b)", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """HTML (or plain text) -> tidy plain text with paragraph breaks."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _secs(match: re.Match) -> int:
    hours, minutes, seconds = match.group(1), int(match.group(2)), int(match.group(3))
    return (int(hours) if hours else 0) * 3600 + minutes * 60 + seconds


def render(text: str, episode: dict, timestamp_param: str = "t") -> Markup:
    """Plain text -> safe HTML paragraphs, with web links and clickable timestamps.

    A timestamp only becomes a link if it falls within the episode (so a date like 10:30am
    in a 5-minute clip stays text). It opens the "Open with…" sheet at that point."""
    if not text:
        return Markup("")
    duration = episode.get("duration_secs") or 0
    youtube = episode.get("source_kind") == "youtube"

    def link_timestamp(match: re.Match) -> str:
        secs = _secs(match)
        label = escape(match.group(0))
        if not episode.get("patreon_url") or (duration and secs > duration + 60):
            return label
        video = build_resume_url(episode["patreon_url"], secs, "t" if youtube else timestamp_param)
        attrs = [f'href="{escape(video)}"', 'target="_blank"', 'rel="noopener"', 'class="ts"',
                 f'data-direct="{escape(video)}"']
        if episode.get("pc_web_url"):
            pc_app = episode["pc_app_url"].split("?")[0] + f"?t={secs}"
            attrs += ["data-choose", f'data-title="{escape(episode["title"])}"',
                      f'data-video-label="{escape(episode.get("source_name", "Patreon"))}"',
                      f'data-video-url="{escape(video)}"', f'data-video-direct="{escape(video)}"',
                      f'data-pc-url="{escape(episode["pc_web_url"])}"', f'data-pc-direct="{escape(pc_app)}"',
                      f'data-position="{label}"', f'data-position-label="Start at {label}"']
        return f'<a {" ".join(attrs)}>{label}</a>'

    def render_line(line: str) -> str:
        out, last = [], 0
        for url in URL.finditer(line):
            out.append(TIMESTAMP.sub(link_timestamp, escape(line[last:url.start()])))
            href = escape(url.group(0))
            out.append(f'<a href="{href}" target="_blank" rel="noopener nofollow">{href}</a>')
            last = url.end()
        out.append(TIMESTAMP.sub(link_timestamp, escape(line[last:])))
        return "".join(out)

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    html = "".join("<p>" + "<br>".join(render_line(line) for line in p.split("\n")) + "</p>" for p in paragraphs)
    return Markup(html)


def cached(conn: sqlite3.Connection, episode_id: int):
    return conn.execute("SELECT * FROM episode_notes WHERE episode_id = ?", (episode_id,)).fetchone()


def fetch(conn: sqlite3.Connection, episode: dict, source_client=None, pocketcasts=None):
    """Fetch and cache both sides' notes. Each side is best-effort: a failure leaves it empty."""
    source_text = pc_text = None
    try:
        if source_client is not None:
            if episode.get("source_kind") == "youtube":
                source_text = source_client.video_description(episode["patreon_post_id"])
            else:
                source_text = html_to_text(source_client.get_post_text(episode["patreon_post_id"]))
    except Exception as exc:  # noqa: BLE001 - notes are a nicety; never break the page
        log.info("Source notes unavailable for episode %s: %s", episode["id"], type(exc).__name__)
    try:
        if pocketcasts is not None and episode.get("pocketcasts_episode_uuid") and episode.get("matched_podcast_uuid"):
            pc_text = html_to_text(pocketcasts.show_notes(episode["matched_podcast_uuid"],
                                                          episode["pocketcasts_episode_uuid"]))
    except Exception as exc:  # noqa: BLE001
        log.info("Pocket Casts notes unavailable for episode %s: %s", episode["id"], type(exc).__name__)
    with conn:
        conn.execute("INSERT OR REPLACE INTO episode_notes (episode_id, source_text, pc_text, fetched_at) "
                     "VALUES (?, ?, ?, ?)", (episode["id"], source_text or None, pc_text or None, utcnow()))
    return cached(conn, episode["id"])
