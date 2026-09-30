"""YouTube Data API v3: discover new videos, fetch details, stats and comments.

Everything here is deterministic. Deduplication is a set lookup against the
`videos` table, never an LLM judgment.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

from . import store

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}


class YouTubeError(RuntimeError):
    """Any API failure that should abort the run without touching state."""


class QuotaExceeded(YouTubeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise YouTubeError("YOUTUBE_API_KEY is not set")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.units_used = 0

    def get(self, endpoint: str, **params) -> dict:
        params["key"] = self.api_key
        try:
            resp = self.session.get(f"{API}/{endpoint}", params=params, timeout=30)
        except requests.RequestException as e:
            raise YouTubeError(f"{endpoint}: network error: {e}") from e
        self.units_used += 1
        if resp.status_code == 200:
            return resp.json()
        reason, message = _error_reason(resp)
        if reason in QUOTA_REASONS:
            raise QuotaExceeded(f"{endpoint}: YouTube quota exhausted ({reason})")
        err = YouTubeError(f"{endpoint}: HTTP {resp.status_code} {reason}: {message}")
        err.reason = reason
        raise err


def _error_reason(resp) -> tuple[str, str]:
    try:
        err = resp.json()["error"]
        return (err.get("errors") or [{}])[0].get("reason", ""), err.get("message", "")
    except Exception:
        return "", resp.text[:200]


# --- helpers --------------------------------------------------------------

_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str | None) -> int | None:
    """ISO-8601 duration (PT1M30S) -> seconds."""
    if not iso:
        return None
    m = _DURATION.fullmatch(iso)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _int(x):
    return int(x) if x is not None else None


def best_thumbnail(thumbs: dict) -> str | None:
    for size in ("maxres", "high", "medium", "default"):
        if size in thumbs:
            return thumbs[size]["url"]
    return None


# --- API steps ------------------------------------------------------------

def uploads_playlist_id(client: YouTubeClient, conn, brand: dict) -> str:
    """Step 1. Cached forever in the `channels` table after first lookup."""
    cid = brand["channel_id"]
    cached = store.get_uploads_playlist(conn, cid)
    if cached:
        return cached
    params = {"forHandle": cid} if cid.startswith("@") else {"id": cid}
    items = client.get("channels", part="contentDetails", **params).get("items", [])
    if not items:
        raise YouTubeError(f"Channel not found for {brand['name']} ({cid})")
    pid = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    store.cache_uploads_playlist(conn, cid, brand["name"], pid)
    return pid


def scan_new_video_ids(client: YouTubeClient, conn, playlist_id: str, since: str | None,
                       max_scan: int) -> list[tuple[str, str]]:
    """Step 2+3. Walk the uploads playlist newest-first and return unknown
    (video_id, published_at) pairs. Stops at the first known video, at anything
    published before `since`, or after `max_scan` items."""
    found: list[tuple[str, str]] = []
    scanned, page = 0, None
    while scanned < max_scan:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = client.get("playlistItems", **params)
        items = data.get("items", [])
        ids = [it["contentDetails"]["videoId"] for it in items]
        known = store.known_video_ids(conn, ids)
        for it in items:
            vid = it["contentDetails"]["videoId"]
            pub = it["contentDetails"].get("videoPublishedAt") or ""
            scanned += 1
            if vid in known or (since and pub and pub < since):
                return found
            found.append((vid, pub))
            if scanned >= max_scan:
                break
        page = data.get("nextPageToken")
        if not page:
            break
    return found


def fetch_video_details(client: YouTubeClient, video_ids: list[str]) -> dict[str, dict]:
    """Step 4. Batched 50 per call. Returns {video_id: details}."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = client.get("videos", part="snippet,statistics,contentDetails",
                          id=",".join(batch), maxResults=50)
        for it in data.get("items", []):
            sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
            out[it["id"]] = {
                "video_id": it["id"],
                "published_at": sn.get("publishedAt"),
                "title": sn.get("title"),
                "description": sn.get("description", ""),
                "tags": sn.get("tags", []),
                "duration_seconds": parse_duration(cd.get("duration")),
                "thumbnail_url": best_thumbnail(sn.get("thumbnails", {})),
                "views": _int(st.get("viewCount")),
                "likes": _int(st.get("likeCount")),
                "comment_count": _int(st.get("commentCount")),
            }
    return out


def fetch_stats(client: YouTubeClient, video_ids: list[str]) -> dict[str, dict]:
    """Refresh view/like/comment counts for known videos (baseline needs current numbers)."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        data = client.get("videos", part="statistics", id=",".join(video_ids[i:i + 50]))
        for it in data.get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"views": _int(st.get("viewCount")), "likes": _int(st.get("likeCount")),
                             "comment_count": _int(st.get("commentCount"))}
    return out


def fetch_comments(client: YouTubeClient, video_id: str, limit: int = 50) -> list[str] | None:
    """Step 5. Returns comment text only (no author names/IDs), or None if disabled.
    The caller must never persist these."""
    try:
        data = client.get("commentThreads", part="snippet", videoId=video_id,
                          order="relevance", maxResults=limit, textFormat="plainText")
    except QuotaExceeded:
        raise
    except YouTubeError as e:
        if getattr(e, "reason", "") in {"commentsDisabled", "forbidden"}:
            return None
        raise
    return [
        it["snippet"]["topLevelComment"]["snippet"].get("textOriginal", "")
        for it in data.get("items", [])
    ]


# --- orchestration for the fetch stage ------------------------------------

@dataclass
class FetchResult:
    new_videos: dict[str, dict] = field(default_factory=dict)  # video_id -> details (+brand)
    overflow: dict[str, int] = field(default_factory=dict)     # brand -> videos deferred
    refreshed: int = 0


def run_fetch(conn, client: YouTubeClient, brands: list[dict], *, per_run_cap: int = 25,
              first_run_backfill: int = 20, max_scan: int = 100,
              refresh_recent: int = 20) -> FetchResult:
    """Fetch everything into memory first, then persist in one transaction.
    Any YouTubeError propagates before a single row is written."""
    result = FetchResult()
    refresh: dict[str, dict] = {}
    playlists: dict[str, str] = {}

    for brand in brands:
        name = brand["name"]
        playlists[name] = uploads_playlist_id(client, conn, brand)
        since = store.latest_published_at(conn, name)
        candidates = scan_new_video_ids(client, conn, playlists[name], since, max_scan)

        if since is None:
            # First run: take the newest N so there is a baseline to compare against.
            chosen = candidates[:first_run_backfill]
        else:
            # Later runs: oldest-first, so deferred ones are picked up next run.
            candidates.sort(key=lambda x: x[1])
            chosen = candidates[:per_run_cap]
        deferred = len(candidates) - len(chosen)
        if deferred > 0 and since is not None:
            result.overflow[name] = deferred
            log.warning("%s: %d new uploads exceed cap of %d; deferring %d to next run",
                        name, len(candidates), per_run_cap, deferred)

        details = fetch_video_details(client, [vid for vid, _ in chosen])
        for vid, d in details.items():
            d["brand"] = name
            result.new_videos[vid] = d

        recent = store.recent_video_ids(conn, name, refresh_recent)
        if recent:
            refresh.update(fetch_stats(client, recent))

    with store.transaction(conn):
        for vid, d in result.new_videos.items():
            store.insert_video(conn, d)
            store.save_metrics(conn, vid, d["views"], d["likes"], d["comment_count"])
        for vid, s in refresh.items():
            store.save_metrics(conn, vid, s["views"], s["likes"], s["comment_count"])
        result.refreshed = len(refresh)
    return result
