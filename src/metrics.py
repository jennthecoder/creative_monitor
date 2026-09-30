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
AGE_MATCH_DAYS = 7      # compare every video's views at the same age
MIN_MATCHED_PEERS = 5   # day-7 readings needed before a format switches to them


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


def _history(conn, brand: str) -> dict[str, list[tuple[datetime, int]]]:
    """Every view snapshot for the brand's videos, oldest first."""
    out: dict[str, list[tuple[datetime, int]]] = {}
    for r in conn.execute(
            """SELECT m.video_id, m.measured_at, m.views FROM metrics m
               JOIN videos v ON v.video_id = m.video_id
               WHERE v.brand = ? AND m.views IS NOT NULL
               ORDER BY m.measured_at""", (brand,)):
        # measured_at is a date; the scheduled run lands mid-day UTC.
        at = _parse(r["measured_at"]) + timedelta(hours=12)
        out.setdefault(r["video_id"], []).append((at, r["views"]))
    return out


def views_at_age(published_at: str, snaps: list[tuple[datetime, int]],
                 age_days: float = AGE_MATCH_DAYS) -> float | None:
    """Views when the video was `age_days` old: a snapshot within a day of that age,
    else linear interpolation between the snapshots either side. None when no
    snapshot brackets it (e.g. videos first seen in the backfill, already older)."""
    target = _parse(published_at) + timedelta(days=age_days)
    near = [(abs(at - target), v) for at, v in snaps if abs(at - target) <= timedelta(days=1)]
    if near:
        return float(min(near)[1])
    before = [(at, v) for at, v in snaps if at < target]
    after = [(at, v) for at, v in snaps if at > target]
    if not before or not after:
        return None
    (t0, v0), (t1, v1) = before[-1], after[0]
    return v0 + (v1 - v0) * (target - t0) / (t1 - t0)


def channel_baseline(conn, brand: str, now: datetime | None = None,
                     n: int = BASELINE_VIDEOS, min_age_days: int = MIN_AGE_DAYS,
                     bucket: str | None = None) -> float | None:
    """Median views across the brand's last `n` videos (optionally of one format
    bucket) that are at least `min_age_days` old. Median, not mean, so one viral
    video can't move it. Views at day 7 once enough peers have that reading."""
    now = now or datetime.now(timezone.utc)
    peers = _peers(conn, brand, now, n, min_age_days, bucket)
    snaps = _history(conn, brand)
    matched = [x for x in (views_at_age(r["published_at"], snaps.get(r["video_id"], []))
                           for r in peers) if x is not None]
    if len(matched) >= MIN_MATCHED_PEERS:
        return float(median(matched))
    views = [r["views"] for r in peers]
    return float(median(views)) if views else None


def _peers(conn, brand, now, n=BASELINE_VIDEOS, min_age_days=MIN_AGE_DAYS, bucket=None):
    return [
        r for r in _latest_rows(conn, brand)
        if r["views"] is not None and not is_too_early(r["published_at"], now, min_age_days)
        and (bucket is None or format_bucket(r["duration_seconds"]) == bucket)
    ][:n]


def engagement_rate(views, likes, comments) -> float | None:
    """None when views are zero or likes are hidden (a comments-only rate would
    understate engagement and isn't comparable)."""
    if not views or likes is None:
        return None
    return (likes + (comments or 0)) / views


def _rate(num, views) -> float | None:
    return num / views if views and num is not None else None


def _ratio(x, base) -> float | None:
    return x / base if x is not None and base else None


def _median_or_none(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(median(xs)) if xs else None


@dataclass
class Score:
    video_id: str
    views: int | None
    view_index: float | None       # views / median of same brand + format; >1.0 = beat its own norm
    engagement_rate: float | None  # (likes + comments) / views
    too_early: bool
    baseline: float | None
    bucket: str = "episode"
    age_matched: bool = False      # index compares views at day 7, not today's totals
    views_d7: float | None = None
    like_rate: float | None = None         # likes / views
    comment_rate: float | None = None      # comments / views
    like_index: float | None = None        # vs the median like rate of the same brand + format
    comment_index: float | None = None
    engagement_index: float | None = None


def score_brand(conn, brand: str, now: datetime | None = None) -> dict[str, Score]:
    """Views are compared at the same age (day 7) per format once at least
    MIN_MATCHED_PEERS peers have that reading. Until then, and for videos with no
    day-7 reading, today's totals are compared (older videos have had longer to
    collect views, so those indices are marked not age-matched).

    Engagement rates use the latest snapshot: ratios settle fast, totals don't."""
    now = now or datetime.now(timezone.utc)
    rows = _latest_rows(conn, brand)
    snaps = _history(conn, brand)
    d7 = {r["video_id"]: views_at_age(r["published_at"], snaps.get(r["video_id"], []))
          for r in rows}

    bases = {}
    for b in BUCKETS:
        peers = _peers(conn, brand, now, bucket=b)
        matched = [d7[r["video_id"]] for r in peers if d7.get(r["video_id"]) is not None]
        bases[b] = {
            "d7": float(median(matched)) if len(matched) >= MIN_MATCHED_PEERS else None,
            "total": _median_or_none(r["views"] for r in peers),
            "like": _median_or_none(_rate(r["likes"], r["views"]) for r in peers),
            "comment": _median_or_none(_rate(r["comment_count"], r["views"]) for r in peers),
            "eng": _median_or_none(engagement_rate(r["views"], r["likes"], r["comment_count"])
                                   for r in peers),
        }

    out = {}
    for r in rows:
        bucket = format_bucket(r["duration_seconds"])
        base = bases[bucket]
        early = is_too_early(r["published_at"], now)
        vd7 = d7.get(r["video_id"])
        if base["d7"] and vd7 is not None:
            idx, baseline, matched = vd7 / base["d7"], base["d7"], True
        elif base["d7"] and early:
            # Early signal: views so far against the day-7 norm (conservative).
            idx, baseline, matched = _ratio(r["views"], base["d7"]), base["d7"], False
        else:
            idx, baseline, matched = _ratio(r["views"], base["total"]), base["total"], False
        lr, cr = _rate(r["likes"], r["views"]), _rate(r["comment_count"], r["views"])
        er = engagement_rate(r["views"], r["likes"], r["comment_count"])
        out[r["video_id"]] = Score(
            video_id=r["video_id"], views=r["views"], view_index=idx,
            engagement_rate=er, too_early=early, baseline=baseline, bucket=bucket,
            age_matched=matched, views_d7=vd7, like_rate=lr, comment_rate=cr,
            like_index=_ratio(lr, base["like"]), comment_index=_ratio(cr, base["comment"]),
            engagement_index=_ratio(er, base["eng"]),
        )
    return out
