import json

import pytest

from src import store, synthesise
from tests.test_classify import FakeClient

VIDEO = {"video_id": "v1", "brand": "Brand A", "title": "Our new sneaker"}
THEMES = {"recurring_themes": ["sizing runs small"], "objections": ["price"],
          "questions": ["is it waterproof?"], "praise": ["colourways"],
          "overall_sentiment": "mixed"}


def test_returns_themes():
    t = synthesise.synthesise_comments(FakeClient(json.dumps(THEMES)), VIDEO, ["runs small", "pricey"])
    assert t["questions"] == ["is it waterproof?"] and t["comments_analysed"] == 2


def test_comments_disabled_skips_call():
    fc = FakeClient()
    assert synthesise.synthesise_comments(fc, VIDEO, None) is None
    assert synthesise.synthesise_comments(fc, VIDEO, []) is None
    assert fc.calls == []


def test_bad_response_raises():
    with pytest.raises(synthesise.SynthesisFailed):
        synthesise.synthesise_comments(FakeClient("garbage"), VIDEO, ["x"])


def test_only_themes_persisted(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.insert_video(conn, {"video_id": "v1", "brand": "A", "published_at": "2026-09-01"})
    raw = ["I'm Jane and these run small", "@bob pricey"]
    t = synthesise.synthesise_comments(FakeClient(json.dumps(THEMES)), VIDEO, raw)
    store.save_comment_themes(conn, "v1", t)
    dump = "\n".join(conn.iterdump())
    assert "Jane" not in dump and "@bob" not in dump
    assert store.get_comment_themes(conn, "v1")["objections"] == ["price"]


def test_disabled_recorded_as_null(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.insert_video(conn, {"video_id": "v1", "brand": "A", "published_at": "2026-09-01"})
    store.save_comment_themes(conn, "v1", None)
    assert store.has_comment_themes(conn, "v1") and store.get_comment_themes(conn, "v1") is None
