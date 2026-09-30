import pytest

from src import fetch, store


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeYouTube:
    """Minimal in-memory stand-in for the YouTube Data API."""

    def __init__(self, n_videos=30, comments_disabled=(), quota_after=None):
        # Newest first, like the uploads playlist.
        self.videos = [
            {"id": f"v{i:03d}", "published": f"2026-09-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z"}
            for i in range(n_videos)
        ]
        self.videos.sort(key=lambda v: v["published"], reverse=True)
        self.comments_disabled = set(comments_disabled)
        self.quota_after, self.calls = quota_after, 0

    def add_upload(self, vid, published):
        self.videos.insert(0, {"id": vid, "published": published})

    def get(self, url, params, timeout):
        self.calls += 1
        if self.quota_after is not None and self.calls > self.quota_after:
            return FakeResp(403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}})
        ep = url.rsplit("/", 1)[1]
        if ep == "channels":
            return FakeResp(200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if ep == "playlistItems":
            start = int(params.get("pageToken", 0))
            page = self.videos[start:start + 50]
            body = {"items": [{"contentDetails": {"videoId": v["id"], "videoPublishedAt": v["published"]}}
                              for v in page]}
            if start + 50 < len(self.videos):
                body["nextPageToken"] = str(start + 50)
            return FakeResp(200, body)
        if ep == "videos":
            ids = params["id"].split(",")
            by_id = {v["id"]: v for v in self.videos}
            return FakeResp(200, {"items": [{
                "id": i,
                "snippet": {"publishedAt": by_id[i]["published"], "title": f"Title {i}",
                            "thumbnails": {"high": {"url": f"https://img/{i}.jpg"}}},
                "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "5"},
                "contentDetails": {"duration": "PT1M5S"},
            } for i in ids if i in by_id]})
        if ep == "commentThreads":
            if params["videoId"] in self.comments_disabled:
                return FakeResp(403, {"error": {"errors": [{"reason": "commentsDisabled"}], "message": "off"}})
            return FakeResp(200, {"items": [{"snippet": {"topLevelComment": {"snippet": {
                "textOriginal": "love it", "authorDisplayName": "Somebody"}}}}]})
        raise AssertionError(ep)


BRANDS = [{"name": "Brand A", "channel_id": "UCaaa"}]


def make(tmp_path, **kw):
    fake = FakeYouTube(**kw)
    return store.connect(tmp_path / "t.db"), fake, fetch.YouTubeClient("k", session=fake)


def test_parse_duration():
    assert fetch.parse_duration("PT1M30S") == 90
    assert fetch.parse_duration("PT2H") == 7200
    assert fetch.parse_duration("P1DT1S") == 86401


def test_second_run_processes_zero(tmp_path):
    conn, fake, client = make(tmp_path)
    r1 = fetch.run_fetch(conn, client, BRANDS, backfill=20)
    assert len(r1.new_videos) == 20  # first-run backfill
    r2 = fetch.run_fetch(conn, client, BRANDS, backfill=20)
    assert len(r2.new_videos) == 0
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 20
    # Same-day rerun doesn't duplicate metrics rows.
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 20


def test_new_upload_is_picked_up(tmp_path):
    conn, fake, client = make(tmp_path)
    fetch.run_fetch(conn, client, BRANDS, backfill=20)
    fake.add_upload("fresh", "2026-09-29T12:00:00Z")
    r = fetch.run_fetch(conn, client, BRANDS, backfill=20)
    assert list(r.new_videos) == ["fresh"]


def test_uploads_playlist_cached(tmp_path):
    conn, fake, client = make(tmp_path)
    fetch.run_fetch(conn, client, BRANDS, backfill=20)
    fetch.run_fetch(conn, client, BRANDS, backfill=20)
    assert store.get_uploads_playlist(conn, "UCaaa") == "UU1"


def test_overflow_is_capped_and_deferred(tmp_path):
    conn, fake, client = make(tmp_path)
    fetch.run_fetch(conn, client, BRANDS, backfill=20)
    for i in range(10):
        fake.add_upload(f"burst{i}", f"2026-09-30T{i:02d}:00:00Z")
    r = fetch.run_fetch(conn, client, BRANDS, per_run_cap=4, backfill=20)
    assert len(r.new_videos) == 4 and r.overflow == {"Brand A": 6}
    r = fetch.run_fetch(conn, client, BRANDS, per_run_cap=4, backfill=20)
    assert len(r.new_videos) == 4
    r = fetch.run_fetch(conn, client, BRANDS, per_run_cap=4, backfill=20)
    assert len(r.new_videos) == 2 and not r.overflow


def test_quota_exhaustion_leaves_state_untouched(tmp_path):
    conn, fake, client = make(tmp_path, quota_after=3)
    with pytest.raises(fetch.QuotaExceeded):
        fetch.run_fetch(conn, client, BRANDS + [{"name": "Brand B", "channel_id": "UCbbb"}], backfill=20)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0


def test_comments_disabled_returns_none(tmp_path):
    conn, fake, client = make(tmp_path, comments_disabled={"v001"})
    assert fetch.fetch_comments(client, "v001") is None
    assert fetch.fetch_comments(client, "v002") == ["love it"]  # text only, no author


def test_raising_backfill_tops_up_existing_brand(tmp_path):
    conn, fake, client = make(tmp_path)
    assert len(fetch.run_fetch(conn, client, BRANDS, backfill=10).new_videos) == 10
    r = fetch.run_fetch(conn, client, BRANDS, backfill=25)
    assert len(r.new_videos) == 15  # the next-oldest 15, none duplicated
    assert len(fetch.run_fetch(conn, client, BRANDS, backfill=25).new_videos) == 0
    assert conn.execute("SELECT COUNT(DISTINCT video_id) FROM videos").fetchone()[0] == 25
