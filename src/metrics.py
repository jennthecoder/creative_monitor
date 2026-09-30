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


def _latest_rows(conn, brand: str):
    """Each video of `brand` with its most recent metrics row, newest first."""
    return conn.execute(
        """SELECT v.video_id, v.published_at, m.views, m.likes, m.comment_count
           FROM videos v
           JOIN metrics m ON m.video_id = v.video_id
           WHERE v.brand = ?
             AND m.measured_at = (SELECT MAX(measured_at) FROM metrics WHERE video_id = v.video_id)
           ORDER BY v.published_at DESC""",
        (brand,),
    ).fetchall()


def channel_baseline(conn, brand: str, now: datetime | None = None,
                     n: int = BASELINE_VIDEOS, min_age_days: int = MIN_AGE_DAYS) -> float | None:
    """Median views across the brand's last `n` videos that are at least
    `min_age_days` old. Median, not mean, so one viral video can't move it."""
    now = now or datetime.now(timezone.utc)
    views = [
        r["views"] for r in _latest_rows(conn, brand)
        if r["views"] is not None and not is_too_early(r["published_at"], now, min_age_days)
    ][:n]
    return float(median(views)) if views else None


def engagement_rate(views, likes, comments) -> float | None:
    if not views:
        return None
    return ((likes or 0) + (comments or 0)) / views


@dataclass
class Score:
    video_id: str
    views: int | None
    view_index: float | None       # views / channel median; >1.0 = beat the brand's own norm
    engagement_rate: float | None  # (likes + comments) / views
    too_early: bool
    baseline: float | None


def score_brand(conn, brand: str, now: datetime | None = None) -> dict[str, Score]:
    now = now or datetime.now(timezone.utc)
    base = channel_baseline(conn, brand, now)
    out = {}
    for r in _latest_rows(conn, brand):
        early = is_too_early(r["published_at"], now)
        idx = (r["views"] / base) if (base and r["views"] is not None) else None
        out[r["video_id"]] = Score(
            video_id=r["video_id"], views=r["views"], view_index=idx,
            engagement_rate=engagement_rate(r["views"], r["likes"], r["comment_count"]),
            too_early=early, baseline=base,
        )
    return out
