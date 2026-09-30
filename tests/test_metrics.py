from datetime import datetime, timedelta, timezone

import pytest

from src import metrics, store

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def seed(conn, brand, rows):
    """rows: list of (video_id, days_old, views, likes, comments)"""
    for vid, age, views, likes, comments in rows:
        pub = (NOW - timedelta(days=age)).isoformat()
        store.insert_video(conn, {"video_id": vid, "brand": brand, "published_at": pub})
        store.save_metrics(conn, vid, views, likes, comments, measured_at="2026-09-30")


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    seed(c, "A", [
        ("a1", 10, 1000, 40, 10),
        ("a2", 20, 2000, 50, 10),
        ("a3", 30, 3000, 60, 30),
        ("viral", 40, 1_000_000, 9000, 1000),
        ("a5", 50, 4000, 10, 10),
        ("fresh", 2, 50, 5, 0),       # too early — excluded from baseline
    ])
    return c


def test_median_ignores_viral_outlier(conn):
    # eligible views: 1000, 2000, 3000, 1_000_000, 4000 -> median 3000
    assert metrics.channel_baseline(conn, "A", NOW) == 3000


def test_recent_videos_excluded_from_baseline(conn):
    seed(conn, "A", [("fresh2", 1, 10, 0, 0), ("fresh3", 3, 10, 0, 0)])
    assert metrics.channel_baseline(conn, "A", NOW) == 3000


def test_median_video_scores_near_one(conn):
    s = metrics.score_brand(conn, "A", NOW)
    assert s["a3"].view_index == pytest.approx(1.0)
    assert s["viral"].view_index > 300
    assert s["a1"].view_index == pytest.approx(1 / 3)


def test_engagement_rate(conn):
    s = metrics.score_brand(conn, "A", NOW)
    assert s["a1"].engagement_rate == pytest.approx(0.05)


def test_fresh_video_flagged(conn):
    s = metrics.score_brand(conn, "A", NOW)
    assert s["fresh"].too_early and not s["a1"].too_early


def test_only_last_20_count(tmp_path):
    c = store.connect(tmp_path / "t.db")
    # 20 recent videos at 100 views, 10 older ones at 10_000 — older ones fall outside the window
    seed(c, "B", [(f"r{i}", 8 + i, 100, 0, 0) for i in range(20)] +
                 [(f"o{i}", 100 + i, 10_000, 0, 0) for i in range(10)])
    assert metrics.channel_baseline(c, "B", NOW) == 100


def test_no_eligible_videos_gives_none(tmp_path):
    c = store.connect(tmp_path / "t.db")
    seed(c, "C", [("new", 1, 100, 1, 1)])
    assert metrics.channel_baseline(c, "C", NOW) is None
    assert metrics.score_brand(c, "C", NOW)["new"].view_index is None


def test_zero_views_engagement_is_none():
    assert metrics.engagement_rate(0, 1, 1) is None
