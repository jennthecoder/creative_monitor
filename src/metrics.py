"""Normalisation and baseline comparison. Pure arithmetic, no LLM.

Raw views measure channel size and video age, not creative quality. Indexing each
video against its own channel's median isolates the creative signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

BASELINE_VIDEOS = 20
MIN_AGE_DAYS = 7


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_too_early(published_at: str, now: datetime, min_age_days: int = MIN_AGE_DAYS) -> bool:
    return now - _parse(published_at) < timedelta(days=min_age_days)


SHORT_MAX_SECONDS = 180    # YouTube Shorts can run up to 3 minutes
CLIP_MAX_SECONDS = 20 * 60  # observed gap between clips and full episodes
BUCKETS = ("short", "clip", "episode")


def format_bucket(duration_seconds: int | None) -> str:
    """Shorts, clips and full episodes live in different view distributions, so each
    gets its own baseline. Duration-based and deterministic, never the LLM's call."""
    if duration_seconds is None:
        return "episode"
    if duration_seconds <= SHORT_MAX_SECONDS:
        return "short"
    return "clip" if duration_seconds <= CLIP_MAX_SECONDS else "episode"


def _latest_rows(conn, brand: str):
    """Each video of `brand` with its most recent metrics row, newest first."""
    return conn.execute(
        """SELECT v.video_id, v.published_at, v.duration_seconds,
                  m.views, m.likes, m.comment_count
           FROM videos v
           JOIN metrics m ON m.video_id = v.video_id
           WHERE v.brand = ?
             AND m.measured_at = (SELECT MAX(measured_at) FROM metrics WHERE video_id = v.video_id)
           ORDER BY v.published_at DESC""",
        (brand,),
    ).fetchall()


def channel_baseline(conn, brand: str, now: datetime | None = None,
                     n: int = BASELINE_VIDEOS, min_age_days: int = MIN_AGE_DAYS,
                     bucket: str | None = None) -> float | None:
    """Median views across the brand's last `n` videos (optionally of one format
    bucket) that are at least `min_age_days` old. Median, not mean, so one viral
    video can't move it."""
    now = now or datetime.now(timezone.utc)
    views = [
        r["views"] for r in _latest_rows(conn, brand)
        if r["views"] is not None and not is_too_early(r["published_at"], now, min_age_days)
        and (bucket is None or format_bucket(r["duration_seconds"]) == bucket)
    ][:n]
    return float(median(views)) if views else None


def engagement_rate(views, likes, comments) -> float | None:
    """None when views are zero or likes are hidden (a comments-only rate would
    understate engagement and isn't comparable)."""
    if not views or likes is None:
        return None
    return (likes + (comments or 0)) / views


@dataclass
class Score:
    video_id: str
    views: int | None
    view_index: float | None       # views / median of same brand + format; >1.0 = beat its own norm
    engagement_rate: float | None  # (likes + comments) / views
    too_early: bool
    baseline: float | None
    bucket: str = "episode"


def score_brand(conn, brand: str, now: datetime | None = None) -> dict[str, Score]:
    now = now or datetime.now(timezone.utc)
    bases = {b: channel_baseline(conn, brand, now, bucket=b) for b in BUCKETS}
    out = {}
    for r in _latest_rows(conn, brand):
        bucket = format_bucket(r["duration_seconds"])
        base = bases[bucket]
        idx = (r["views"] / base) if (base and r["views"] is not None) else None
        out[r["video_id"]] = Score(
            video_id=r["video_id"], views=r["views"], view_index=idx,
            engagement_rate=engagement_rate(r["views"], r["likes"], r["comment_count"]),
            too_early=is_too_early(r["published_at"], now), baseline=base, bucket=bucket,
        )
    return out
