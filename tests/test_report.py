from datetime import datetime, timedelta, timezone

from src import report, store
from src.config import load_rubric

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
RUBRIC = load_rubric()
VALS = {"hook_type": "problem", "format": "ugc", "angle": "problem_solution", "offer": "none",
        "production_level": "low", "thumbnail_treatment": "face"}


def add(conn, vid, age, views, conf="high", values=VALS):
    store.insert_video(conn, {"video_id": vid, "brand": "A", "title": f"T-{vid}",
                              "published_at": (NOW - timedelta(days=age)).isoformat()})
    store.save_metrics(conn, vid, views, views // 20, views // 100, measured_at="2026-09-30")
    if values:
        store.save_classification(conn, vid, values, conf, "r", "1")


def digest(conn):
    return report.build_digest(conn, RUBRIC, [{"name": "A", "channel_id": "x"}], NOW)


def test_digest_leads_with_insight(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for i, age in enumerate(range(20, 40, 2)):
        add(conn, f"old{i}", age, 1000)
    add(conn, "hit", 10, 3000)
    add(conn, "new", 2, 100)
    md = digest(conn)
    assert '**Outperforming:** "T-hit" — 3.0x the full-episode median' in md
    assert "UGC · " in md and "no offer" in md
    assert '"T-new"' in md and "too early to judge" in md
    assert "|" not in md  # no raw data tables


def test_low_confidence_flagged(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    add(conn, "shaky", 3, 100, conf="low")
    md = digest(conn)
    assert "### Flagged" in md and '"T-shaky"' in md.split("### Flagged")[1]


def test_no_baseline_message(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    add(conn, "only", 2, 100)
    assert "No baseline yet" in digest(conn)


def test_label_falls_back_for_unknown_dimension():
    assert report.label("pet_shown", "golden_retriever") == "golden retriever"
