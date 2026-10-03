"""Phase 0 probe for Patreon's internal web API (spec section 4.2).

Reads the session cookie from PATREON_SESSION_ID (env or .env) and answers:
  1. Session check: does /api/current_user distinguish logged in vs out?
  2. Bulk progress: can the post list return media IDs + progress in one call?
  3. Post -> media mapping: where does a single post expose its media ID?
  4. Collections discovery: which query lists a campaign's collections?
  5. Audio-only posts: same progress shape as video?

Prints only field paths, types, booleans, counts and safe enum values.
Never prints the cookie, tokens or URLs.

Usage:  python scripts/probe_patreon.py
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

from _probe_common import Http, dig, heading, load_dotenv, require_env, show_paths

load_dotenv()

CAMPAIGN_ID = os.environ.get("PATREON_CAMPAIGN_ID", "14434926")
COLLECTION_ID = os.environ.get("PATREON_COLLECTION_ID", "1909234")
SAMPLE_POST_ID = os.environ.get("PATREON_SAMPLE_POST_ID", "171048709")
SAMPLE_MEDIA_ID = os.environ.get("PATREON_SAMPLE_MEDIA_ID", "755348961")

JSONAPI = {
    "json-api-version": "1.0",
    "json-api-use-default-includes": "false",
    "json-api-use-default-fields": "false",
}
POST_FIELDS = "title,post_type,published_at,url,media_file_duration_seconds,thumbnail"


def has_position(media_obj: dict) -> bool:
    return dig(media_obj, "attributes", "display", "progress", "position_secs") is not None


def progress_summary(media_obj: dict) -> str:
    prog = dig(media_obj, "attributes", "display", "progress", default={})
    return (
        f"media_type={dig(media_obj, 'attributes', 'media_type')!r} "
        f"has_position_secs={'position_secs' in prog} "
        f"has_updated_at={'updated_at' in prog} "
        f"is_watched={prog.get('is_watched')!r} watch_state={prog.get('watch_state')!r}"
    )


def posts_query(extra: dict[str, str], size: int = 5) -> str:
    params = {
        "fields[post]": POST_FIELDS,
        "filter[campaign_id]": CAMPAIGN_ID,
        "filter[collection_id]": COLLECTION_ID,
        "filter[is_suspended]": "false",
        "filter[include_drops]": "true",
        "filter[is_published]": "true",
        "sort": "collection_order",
        "page[size]": str(size),
        **JSONAPI,
    }
    params.update(extra)
    return "/api/posts?" + urlencode(params, safe="[],")


def probe_session(http: Http) -> bool:
    heading("1. Session check: GET /api/current_user")
    status, body = http.request("GET", "/api/current_user?" + urlencode(JSONAPI))
    logged_in = status == 200 and bool(dig(body, "data", "id"))
    print(f"  with cookie:    HTTP {status}, data.id present={bool(dig(body, 'data', 'id'))}, "
          f"data.type={dig(body, 'data', 'type')!r}")
    status_out, body_out = http.request(
        "GET", "/api/current_user?" + urlencode(JSONAPI), omit_headers=("Cookie",)
    )
    print(f"  without cookie: HTTP {status_out}, data.id present={bool(dig(body_out, 'data', 'id'))}, "
          f"has errors={bool(dig(body_out, 'errors'))}")
    print(f"  => usable as health check: {logged_in and not dig(body_out, 'data', 'id')}")
    return logged_in


def probe_media_logged_out(http: Http) -> None:
    heading("1b. Logged-out media shape (control)")
    status, body = http.request("GET", f"/api/media/{SAMPLE_MEDIA_ID}", omit_headers=("Cookie",))
    print(f"  without cookie: HTTP {status}; {progress_summary(dig(body, 'data', default={}))}")
    status, body = http.request("GET", f"/api/media/{SAMPLE_MEDIA_ID}")
    print(f"  with cookie:    HTTP {status}; {progress_summary(dig(body, 'data', default={}))}")


BULK_VARIANTS = {
    "A fields[post]+=post_file": {"fields[post]": POST_FIELDS + ",post_file"},
    "B include=media": {"include": "media", "fields[media]": "media_type,display,owner_id"},
    "C include=audio,media": {"include": "audio,media", "fields[media]": "media_type,display"},
    "D post_file + include=media": {
        "fields[post]": POST_FIELDS + ",post_file",
        "include": "media",
        "fields[media]": "media_type,display",
    },
}


def probe_bulk(http: Http) -> set[str]:
    heading("2. Bulk progress on the post list")
    media_ids: set[str] = set()
    for label, extra in BULK_VARIANTS.items():
        status, body = http.request("GET", posts_query(extra))
        posts = dig(body, "data", default=[]) or []
        included = dig(body, "included", default=[]) or []
        media = [i for i in included if i.get("type") == "media"]
        post_file_ids = [dig(p, "attributes", "post_file", "media_id") for p in posts]
        rel_media = [dig(p, "relationships", "media", "data") for p in posts]
        for pid in post_file_ids:
            if pid:
                media_ids.add(str(pid))
        for rel in rel_media:
            for r in rel if isinstance(rel, list) else ([rel] if rel else []):
                if r.get("id"):
                    media_ids.add(str(r["id"]))
        print(f"\n  [{label}] HTTP {status}: posts={len(posts)}, included media={len(media)}, "
              f"media with position_secs={sum(has_position(m) for m in media)}, "
              f"posts with post_file.media_id={sum(bool(x) for x in post_file_ids)}, "
              f"posts with relationships.media={sum(bool(x) for x in rel_media)}")
        pf = dig(posts, 0, "attributes", "post_file")
        if pf:
            print("    post_file paths:")
            show_paths(pf, indent="      ")
            print(f"    post_file has progress: {dig(pf, 'progress') is not None}")
        if media:
            print("    first included media paths:")
            show_paths(media[0], indent="      ", limit=40)
        if dig(body, "errors"):
            print("    errors:")
            show_paths(dig(body, "errors"), indent="      ", limit=10)
    has_cursor = None
    status, body = http.request("GET", posts_query({}, size=2))
    has_cursor = dig(body, "meta", "pagination", "cursors", "next") is not None
    print(f"\n  pagination: meta.pagination.cursors.next present={has_cursor}, "
          f"total={dig(body, 'meta', 'pagination', 'total')!r}")
    return media_ids


def probe_post_mapping(http: Http) -> set[str]:
    heading("3. Post -> media mapping: GET /api/posts/{id}")
    media_ids: set[str] = set()
    status, body = http.request("GET", f"/api/posts/{SAMPLE_POST_ID}")
    print(f"  default fields: HTTP {status}")
    attrs = dig(body, "data", "attributes", default={})
    for key in sorted(attrs):
        if any(s in key for s in ("media", "post_file", "audio", "video", "metadata", "can_view")):
            val = attrs[key]
            kind = type(val).__name__
            print(f"    attributes.{key}: {kind}")
            if isinstance(val, dict):
                show_paths(val, indent="      ", limit=20)
    pf_id = dig(attrs, "post_file", "media_id")
    if pf_id:
        media_ids.add(str(pf_id))
    print(f"    attributes.post_file.media_id present={pf_id is not None}, "
          f"matches sample media={str(pf_id) == SAMPLE_MEDIA_ID}")
    rels = dig(body, "data", "relationships", default={})
    print(f"    relationships keys: {sorted(rels)}")
    for name in ("media", "audio", "attachments_media"):
        rel = dig(rels, name, "data")
        items = rel if isinstance(rel, list) else ([rel] if rel else [])
        ids = [str(r.get("id")) for r in items if r.get("id")]
        media_ids.update(ids)
        if items:
            print(f"    relationships.{name}: {len(ids)} ids, includes sample media={SAMPLE_MEDIA_ID in ids}")
    return media_ids


COLLECTION_VARIANTS = {
    "A /api/collection filter[campaign_id]": "/api/collection?" + urlencode(
        {"filter[campaign_id]": CAMPAIGN_ID, "fields[collection]": "title,num_posts", **JSONAPI}, safe="[],"),
    "B /api/campaigns/{id}/collections": f"/api/campaigns/{CAMPAIGN_ID}/collections?" + urlencode(
        {"fields[collection]": "title,num_posts", **JSONAPI}, safe="[],"),
    "C /api/collections filter[campaign_id]": "/api/collections?" + urlencode(
        {"filter[campaign_id]": CAMPAIGN_ID, "fields[collection]": "title,num_posts", **JSONAPI}, safe="[],"),
}


def probe_collections(http: Http) -> None:
    heading("4. Collections discovery")
    for label, path in COLLECTION_VARIANTS.items():
        status, body = http.request("GET", path)
        items = dig(body, "data", default=[])
        items = items if isinstance(items, list) else []
        print(f"  [{label}] HTTP {status}: {len(items)} collections")
        # Collection titles and IDs are not secret; print them so the Hidden Cache ID can be confirmed.
        for item in items[:30]:
            print(f"    id={item.get('id')} title={dig(item, 'attributes', 'title')!r} "
                  f"num_posts={dig(item, 'attributes', 'num_posts')!r}")
        if items:
            print(f"    configured collection {COLLECTION_ID} present: "
                  f"{any(str(i.get('id')) == COLLECTION_ID for i in items)}")
            break


def probe_media_types(http: Http, media_ids: set[str]) -> None:
    heading("5. Progress shape by media type")
    ids = list(dict.fromkeys([SAMPLE_MEDIA_ID, *sorted(media_ids)]))[:6]
    seen: dict[str, int] = {}
    for mid in ids:
        status, body = http.request("GET", f"/api/media/{mid}")
        data = dig(body, "data", default={})
        mtype = dig(data, "attributes", "media_type") or "?"
        seen[mtype] = seen.get(mtype, 0) + 1
        print(f"  media #{ids.index(mid) + 1}: HTTP {status}; {progress_summary(data)}")
    print(f"  media types seen: {seen}")
    if "audio" not in seen:
        print("  ! No audio media found in this sample. Set PATREON_SAMPLE_MEDIA_ID to an audio post's media to check.")


def main() -> None:
    cookie = require_env("PATREON_SESSION_ID")
    http = Http("https://www.patreon.com", {"Cookie": f"session_id={cookie}"})
    print(f"Probing campaign={CAMPAIGN_ID} collection={COLLECTION_ID} "
          f"post={SAMPLE_POST_ID} media={SAMPLE_MEDIA_ID}")
    if not probe_session(http):
        print("\nSession does not look logged in. Refresh PATREON_SESSION_ID and rerun.")
        return
    probe_media_logged_out(http)
    media_ids = probe_bulk(http)
    media_ids |= probe_post_mapping(http)
    probe_collections(http)
    probe_media_types(http, media_ids)
    print("\nDone. Paste this output back to Claude.")


if __name__ == "__main__":
    main()
