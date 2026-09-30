import json
from types import SimpleNamespace

import pytest

from src import classify, deliver, fetch, main, store
from tests.test_classify import GOOD
from tests.test_fetch import FakeYouTube
from tests.test_synthesise import THEMES


def test_slack_mrkdwn_conversion():
    md = "## Week of X\n**Outperforming:** \"t\" — 2x  \n*(low confidence)*"
    out = deliver.to_slack_mrkdwn(md)
    assert out.startswith("*Week of X*") and "*Outperforming:*" in out and "_(low confidence)_" in out


def test_file_fallback_without_slack(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(deliver, "REPORTS", tmp_path)
    r = deliver.deliver("# hi", "2026-09-30")
    assert r == {"file": str(tmp_path / "digest-2026-09-30.md"), "slack": False}


def test_slack_post(monkeypatch):
    sent = []
    fake = SimpleNamespace(post=lambda url, json, timeout: sent.append(json) or SimpleNamespace(status_code=200))
    deliver.post_slack("**hi**", "https://hooks.example", session=fake)
    assert sent == [{"text": "*hi*"}]


class SmartLLM:
    """Answers classification or synthesis based on the system prompt (OpenAI shape)."""

    def __init__(self, fail_ids=()):
        self.fail_ids, self.calls = set(fail_ids), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, messages, **kw):
        self.calls += 1
        system = messages[0]["content"]
        text = " ".join(p.get("text", "") for p in messages[1]["content"])
        if "classify" in system:
            body = "broken" if any(f in text for f in self.fail_ids) else json.dumps(GOOD)
        else:
            body = json.dumps(THEMES)
        msg = SimpleNamespace(content=body, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


@pytest.fixture
def env(tmp_path, monkeypatch):
    yt = FakeYouTube(comments_disabled={"v012"})
    claude = SmartLLM(fail_ids={"Title v010"})
    db = tmp_path / "m.db"
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("RUBRIC_FILE", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main.store, "connect", lambda: _connect(db))
    monkeypatch.setattr(main.fetch, "YouTubeClient", lambda key: _yt(key, yt))
    monkeypatch.setattr(main.openai, "OpenAI", lambda: claude)
    monkeypatch.setattr(classify, "fetch_thumbnail", lambda url: None)
    monkeypatch.setattr(deliver, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(main, "SYNTH_MAX_AGE_DAYS", 10_000)
    return SimpleNamespace(db=db, yt=yt, claude=claude, tmp=tmp_path)


_real_connect = store.connect
_real_client = fetch.YouTubeClient


def _connect(db):
    return _real_connect(db)


def _yt(key, fake):
    return _real_client(key, session=fake)


def test_end_to_end_and_rerun(env):
    assert main.run() == 0
    conn = _connect(env.db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    assert q("SELECT COUNT(*) FROM videos") == 30               # whole fake channel (< backfill)
    assert q("SELECT COUNT(*) FROM classifications") == 29      # v010 malformed twice
    assert q("SELECT COUNT(*) FROM quarantine") == 1
    assert q("SELECT COUNT(*) FROM comment_themes WHERE themes_json IS NULL") == 1  # v012 disabled
    run1 = dict(conn.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchone())
    assert run1["videos_found"] == 30 and run1["errors"] == 1 and run1["status"] == "ok_with_errors"
    assert list((env.tmp / "reports").glob("digest-*.md"))

    calls_before = env.claude.calls
    assert main.run() == 0
    assert q("SELECT COUNT(*) FROM videos") == 30
    assert q("SELECT COUNT(*) FROM classifications") == 29
    assert env.claude.calls == calls_before  # nothing re-classified or re-synthesised
    run2 = dict(conn.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchone())
    assert run2["videos_found"] == 0 and run2["errors"] == 0


def test_quota_failure_exits_1_and_records_run(env):
    env.yt.quota_after = 0
    assert main.run() == 1
    conn = _connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    r = conn.execute("SELECT status, message FROM runs").fetchone()
    assert r["status"] == "failed" and "quota" in r["message"]


def test_daily_stats_refresh_then_weekly_run_classifies(env):
    assert main.refresh_stats() == 0
    conn = _connect(env.db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    assert q("SELECT COUNT(*) FROM videos") == 30
    assert q("SELECT COUNT(*) FROM metrics") == 30
    assert q("SELECT COUNT(*) FROM classifications") == 0
    assert env.claude.calls == 0                      # YouTube only, no LLM
    r = conn.execute("SELECT status, message FROM runs").fetchone()
    assert r["status"] == "ok" and r["message"].startswith("stats refresh")
    assert not list((env.tmp / "reports").glob("digest-*.md"))

    assert main.run() == 0                            # weekly run picks up the backlog
    assert q("SELECT COUNT(*) FROM classifications") == 29


def test_daily_stats_refresh_failure_leaves_state(env):
    env.yt.quota_after = 0
    assert main.refresh_stats() == 1
    conn = _connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
