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


def test_shorts_and_long_form_have_separate_baselines(tmp_path):
    c = store.connect(tmp_path / "t.db")
    for i in range(5):  # Shorts ~10k, episodes ~1M
        for vid, dur, views in [(f"s{i}", 45, 10_000), (f"e{i}", 3600, 1_000_000)]:
            store.insert_video(c, {"video_id": vid, "brand": "P", "duration_seconds": dur,
                                   "published_at": (NOW - timedelta(days=10 + i)).isoformat()})
            store.save_metrics(c, vid, views, 1, 1, measured_at="2026-09-30")
    s = metrics.score_brand(c, "P", NOW)
    assert s["e0"].view_index == pytest.approx(1.0) and s["e0"].bucket == "episode"
    assert s["s0"].view_index == pytest.approx(1.0) and s["s0"].bucket == "short"


def test_bucket_boundary():
    assert metrics.format_bucket(180) == "short" and metrics.format_bucket(181) == "clip"
    assert metrics.format_bucket(1200) == "clip" and metrics.format_bucket(1201) == "episode"
    assert metrics.format_bucket(None) == "episode"


def test_hidden_likes_engagement_is_none():
    assert metrics.engagement_rate(1000, None, 5) is None


# --- age matching --------------------------------------------------------

def _snap(day):
    return datetime(2026, 9, day, 12, tzinfo=timezone.utc)


def test_views_at_age_interpolates_between_snapshots():
    pub = datetime(2026, 9, 1, 12, tzinfo=timezone.utc).isoformat()
    # seen at day 4 (400 views) and day 11 (1100 views) -> day 7 = 700
    assert metrics.views_at_age(pub, [(_snap(5), 400), (_snap(12), 1100)]) == pytest.approx(700)


def test_views_at_age_uses_snapshot_near_day_7():
    pub = datetime(2026, 9, 1, 12, tzinfo=timezone.utc).isoformat()
    assert metrics.views_at_age(pub, [(_snap(8), 900), (_snap(15), 5000)]) == 900


def test_views_at_age_none_when_first_seen_later():
    pub = datetime(2026, 9, 1, 12, tzinfo=timezone.utc).isoformat()
    assert metrics.views_at_age(pub, [(_snap(20), 900)]) is None


def seed_history(conn, brand, vid, pub, snaps, likes=None, comments=None):
    """snaps: list of (date 'YYYY-MM-DD', views)"""
    store.insert_video(conn, {"video_id": vid, "brand": brand, "published_at": pub.isoformat()})
    for day, views in snaps:
        store.save_metrics(conn, vid, views, likes if likes is not None else views // 20,
                           comments if comments is not None else views // 100, measured_at=day)


def test_day_7_comparison_removes_age_bias(tmp_path):
    """Five videos all had 1000 views at day 7. The oldest has since grown to 9000,
    which today's totals would call a breakout; at day 7 it is exactly on par."""
    c = store.connect(tmp_path / "t.db")
    for i in range(5):
        pub = NOW - timedelta(days=14 + i, hours=12)
        seed_history(c, "D", f"v{i}", pub, [
            ((pub + timedelta(days=7)).date().isoformat(), 1000),
            ("2026-09-30", 9000 if i == 4 else 1500)])
    s = metrics.score_brand(c, "D", NOW)
    assert s["v4"].age_matched and s["v4"].view_index == pytest.approx(1.0)
    assert s["v4"].baseline == 1000


def test_falls_back_to_totals_until_enough_day_7_readings(conn):
    s = metrics.score_brand(conn, "A", NOW)  # single snapshot -> no day-7 readings
    assert not s["a3"].age_matched and s["a3"].view_index == pytest.approx(1.0)


# --- engagement -----------------------------------------------------------

def test_like_and_comment_rates_indexed_against_channel(conn):
    s = metrics.score_brand(conn, "A", NOW)
    # like rates of eligible videos: .04, .025, .02, .009, .0025 -> median .02
    assert s["a1"].like_rate == pytest.approx(0.04)
    assert s["a1"].like_index == pytest.approx(2.0)
    assert s["a1"].comment_rate == pytest.approx(0.01)
    assert s["a1"].engagement_index is not None
