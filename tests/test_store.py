from src import store
from src.config import load_brands, load_rubric


def test_write_and_read_video(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    v = {"video_id": "abc", "brand": "Brand A", "published_at": "2026-09-01T00:00:00Z",
         "title": "Hello", "duration_seconds": 30}
    assert store.insert_video(conn, v) is True
    assert store.insert_video(conn, v) is False  # rerun-safe
    got = store.get_video(conn, "abc")
    assert got["title"] == "Hello" and got["duration_seconds"] == 30


def test_metrics_same_day_no_duplicate(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.insert_video(conn, {"video_id": "x", "brand": "B", "published_at": "2026-09-01"})
    store.save_metrics(conn, "x", 10, 1, 1)
    store.save_metrics(conn, "x", 20, 2, 2)
    n = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    assert n == 1 and store.latest_metrics(conn, "x")["views"] == 20


def test_config_loads():
    assert load_brands()
    r = load_rubric()
    assert "hook_type" in r["dimensions"] and r["version"]
