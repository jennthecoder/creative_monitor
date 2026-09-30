import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src import digest, store
from web import app as webapp

CONFIG = Path(__file__).resolve().parent.parent / "config"
NOW = datetime.now(timezone.utc)
VALS = {"hook_type": "problem", "format": "ugc", "angle": "problem_solution", "offer": "none",
        "production_level": "low", "thumbnail_treatment": "face"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired to a temp DB and a temp copy of config/, so tests never touch real files."""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG, cfg)
    (cfg / "brands.yaml").write_text(
        "brands:\n  - name: The Test Channel\n    channel_id: UCtest\n")
    db = tmp_path / "t.db"
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(db))
    monkeypatch.setattr(webapp, "CONFIG_DIR", cfg)
    monkeypatch.setattr(webapp, "load_brands",
                        lambda: yaml.safe_load((cfg / "brands.yaml").read_text())["brands"])
    monkeypatch.setattr(webapp, "load_rubric",
                        lambda path=None: _load(path or cfg / "rubric.yaml"))
    monkeypatch.setenv("RUBRIC_FILE", "rubric.yaml")

    conn = real_connect(db)
    for i, (age, views, dur) in enumerate([(10, 9000, 45), (12, 1000, 45), (14, 1100, 45),
                                           (16, 1000, 45), (2, 500, 3600)]):
        vid = f"vid{i}"
        store.insert_video(conn, {"video_id": vid, "brand": "The Test Channel", "title": f"Video {i}",
                                  "duration_seconds": dur,
                                  "published_at": (NOW - timedelta(days=age)).isoformat()})
        store.save_metrics(conn, vid, views, views // 20, views // 100)
        store.save_classification(conn, vid, VALS, "high", "because", "1")
    store.save_comment_themes(conn, "vid0", {"objections": ["price"], "questions": ["sizing"],
                                             "praise": ["colour"], "recurring_themes": [],
                                             "overall_sentiment": "mixed", "comments_analysed": 50})
    store.save_comment_themes(conn, "vid1", None)
    conn.commit()
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    c.cfg = cfg
    return c


def _load(path):
    from src.config import load_rubric
    return load_rubric(path)


def test_every_page_renders(client):
    for url in ["/", "/?week=2026-09-01", "/channel/the-test-channel",
                "/channel/the-test-channel?format=short", "/video/vid0", "/video/vid1",
                "/rubric", "/api/digest"]:
        assert client.get(url).status_code == 200, url


def test_unknown_things_404(client):
    assert client.get("/channel/nope").status_code == 404
    assert client.get("/video/nope").status_code == 404
    assert client.get("/?week=not-a-date").status_code == 400


def test_digest_page_marks_outperformer_with_accent_only(client):
    html = client.get("/").get_data(as_text=True)
    assert "Video 0" in html and 'class="perf out' in html  # 9000 vs ~1050 median
    assert "What viewers" not in html  # themes cluster lives on the video page


def test_video_page_theme_cluster_and_comments_off(client):
    html = client.get("/video/vid0").get_data(as_text=True)
    assert "Objections" in html and "price" in html
    html = client.get("/video/vid1").get_data(as_text=True)
    assert "Comments are turned off on this video" in html


def test_failed_run_banner(client):
    conn = store.connect()
    rid = store.start_run(conn)
    store.finish_run(conn, rid, 0, 0, 1, status="failed", message="YouTube quota exhausted")
    html = client.get("/").get_data(as_text=True)
    assert "The last run failed partway" in html and "YouTube quota exhausted" in html


def test_rubric_save_bumps_version_and_keeps_order(client):
    before = yaml.safe_load((client.cfg / "rubric.yaml").read_text())
    dims = {k: v["values"] for k, v in before["dimensions"].items()}
    dims["offer"].append("Free Gift")          # normalised to free_gift
    dims["music"] = ["licensed", "original"]   # new dimension goes last
    r = client.post("/rubric", data={"dimensions": json.dumps(dims)})
    assert r.status_code == 302
    after = yaml.safe_load((client.cfg / "rubric.yaml").read_text())
    assert after["version"] == "2"
    assert list(after["dimensions"]) == list(before["dimensions"]) + ["music"]
    assert after["dimensions"]["offer"]["values"][-1] == "free_gift"
    assert (client.cfg / "rubric.yaml").read_text().startswith("#")  # header comments kept


def test_rubric_unchanged_save_does_not_bump(client):
    before = (client.cfg / "rubric.yaml").read_text()
    dims = {k: v["values"] for k, v in yaml.safe_load(before)["dimensions"].items()}
    client.post("/rubric", data={"dimensions": json.dumps(dims)})
    assert (client.cfg / "rubric.yaml").read_text() == before


@pytest.mark.parametrize("dims,msg", [
    ({}, "at least one dimension"),
    ({"hook": ["only_one"]}, "at least two"),
    ({"hook": ["a", "a"]}, "appears twice"),
    ({"Bad Name!": ["a", "b"]}, "valid dimension name"),
    ({"hook": ["<script>", "b"]}, "valid value"),
])
def test_rubric_validation_rejects_and_keeps_file(client, dims, msg):
    before = (client.cfg / "rubric.yaml").read_text()
    r = client.post("/rubric", data={"dimensions": json.dumps(dims)})
    assert r.status_code == 400 and msg in r.get_data(as_text=True)
    assert (client.cfg / "rubric.yaml").read_text() == before
    if "<script>" in json.dumps(dims):  # rejected input is echoed escaped, never raw
        assert "&lt;script&gt;" in r.get_data(as_text=True)


def test_bump_version():
    assert webapp.bump_version("1") == "2"
    assert webapp.bump_version("podcast-9") == "podcast-10"
    assert webapp.bump_version("beta") == "beta-2"


def test_format_helpers():
    assert digest.fmt_views(1_891_981) == "1.9M" and digest.fmt_views(48_644) == "48K"
    assert digest.fmt_views(950) == "950" and digest.fmt_duration(9524) == "2:38:44"
    assert digest.bar_position(1.0) == 0.5 and digest.bar_position(100) == 1.0
    assert digest.perf_class(1.5) == "out" and digest.perf_class(1.2) == "above"
    assert digest.perf_class(0.8) == "below" and digest.perf_class(0.5) == "under"
