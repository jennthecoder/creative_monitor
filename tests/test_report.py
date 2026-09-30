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


# --- insight quality ------------------------------------------------------

from src import digest as dg  # noqa: E402
from src.metrics import Score  # noqa: E402


def _score(idx, eng_idx=1.0, early=False, matched=True):
    return Score(video_id="x", views=1, view_index=idx, engagement_rate=0.05, too_early=early,
                 baseline=1, age_matched=matched, engagement_index=eng_idx)


def test_read_separates_packaging_from_content():
    assert dg.read(_score(2.0, 0.6))["key"] == "packaging"
    assert dg.read(_score(2.0, 1.4))["key"] == "both"
    assert dg.read(_score(0.7, 1.8))["key"] == "underpackaged"
    assert dg.read(_score(1.1, 1.0)) is None
    assert dg.read(_score(3.0, 0.5, early=True)) is None


def _v(vid, hook, conf="high"):
    return {"video_id": vid, "cls": {"values": {"hook_type": hook}, "confidence": conf}}


RUB = {"dimensions": {"hook_type": {"values": ["question", "demo"]}}}


def test_patterns_use_hit_rate_against_channel_base_rate():
    # question: 3 of 4 hit. demo: 1 of 6 hit (one viral video). Base rate 4 of 10.
    videos = [_v(f"q{i}", "question") for i in range(4)] + [_v(f"d{i}", "demo") for i in range(6)]
    scores = {f"q{i}": _score(x) for i, x in enumerate([2.0, 1.8, 1.6, 0.9])}
    scores |= {f"d{i}": _score(x) for i, x in enumerate([40.0, 0.9, 0.8, 1.0, 0.7, 1.1])}
    ps = dg.attribute_patterns(videos, scores, RUB)
    assert [p["label"] for p in ps] == ["question hook"]
    assert (ps[0]["hits"], ps[0]["n"]) == (3, 4)
    assert "3 of 4 beat 1.5x" in ps[0]["text"] and "40%" in ps[0]["text"]


def test_low_confidence_and_early_videos_excluded_from_patterns():
    videos = [_v(f"q{i}", "question", conf="low") for i in range(4)] + [_v("e", "question")]
    scores = {f"q{i}": _score(3.0) for i in range(4)} | {"e": _score(3.0, early=True)}
    assert dg.attribute_patterns(videos, scores, RUB) == []


def test_cross_channel_marks_patterns_confirmed_on_both():
    p = lambda: {"key": "hook_type:question", "label": "question hook", "hits": 3, "n": 4}  # noqa: E731
    sections = [{"name": "A", "winners": [p()]}, {"name": "B", "winners": [p()]},
                {"name": "C", "winners": []}]
    out = dg.cross_channel(sections)
    assert out[0]["brands"] == ["A", "B"] and out[0]["hits"] == 6 and out[0]["n"] == 8
    assert sections[0]["winners"][0]["confirmed_by"] == ["B"]
