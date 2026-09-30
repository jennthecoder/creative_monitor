"""Weekly run: fetch -> classify -> measure -> synthesise -> report -> deliver.

    python -m src.main [--skip-synthesis] [--no-deliver]
    python -m src.main --stats-only     # daily: new uploads + view snapshots, no LLM

Daily snapshots give every video an exact day-7 reading, which the performance
index compares on. New uploads found daily are classified by the next weekly run.

Exit code 1 if the fetch stage fails (state untouched); per-video classification
or synthesis failures are counted in the run's error total and never fail the run.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openai
from dotenv import load_dotenv

from . import classify, deliver, fetch, report, store, synthesise
from .config import load_brands, load_rubric

log = logging.getLogger("monitor")
SYNTH_MAX_AGE_DAYS = 21  # themes only appear in the digest for recent videos
LLM_WORKERS = int(os.getenv("LLM_WORKERS", "6"))


def run(skip_synthesis: bool = False, no_deliver: bool = False) -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    brands, rubric = load_brands(), load_rubric()
    conn = store.connect()
    run_id = store.start_run(conn)
    errors = classified = 0

    try:
        yt = fetch.YouTubeClient(os.getenv("YOUTUBE_API_KEY", ""))
        if not os.getenv("OPENAI_API_KEY"):
            raise fetch.YouTubeError("OPENAI_API_KEY is not set")
        result = fetch.run_fetch(conn, yt, brands)
    except fetch.YouTubeError as e:
        conn.rollback()
        log.error("Fetch failed, state left untouched: %s", e)
        store.finish_run(conn, run_id, 0, 0, 1, status="failed", message=str(e))
        return 1
    log.info("Fetched %d new videos (%d stats refreshed, %d YouTube units)",
             len(result.new_videos), result.refreshed, yt.units_used)

    ai = openai.OpenAI()
    details = dict(result.new_videos)

    # --- classify (includes anything left unclassified by an earlier run) ---
    pending = store.unclassified_video_ids(conn)
    missing = [v for v in pending if v not in details]
    if missing:
        try:
            details.update({k: {**d, "brand": store.get_video(conn, k)["brand"]}
                            for k, d in fetch.fetch_video_details(yt, missing).items()})
        except fetch.YouTubeError as e:
            log.error("Could not refetch details for %d pending videos: %s", len(missing), e)
            errors += 1
    def _classify(vid):
        video = details[vid]
        thumb = classify.fetch_thumbnail(video.get("thumbnail_url"))
        return classify.classify_video(ai, video, rubric, thumb)

    # LLM calls run in parallel; all DB writes stay on this thread (SQLite).
    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        futures = {pool.submit(_classify, vid): vid for vid in pending if vid in details}
        for fut in as_completed(futures):
            vid = futures[fut]
            try:
                c = fut.result()
                store.save_classification(conn, vid, c.values, c.confidence, c.rationale,
                                          rubric["version"])
                conn.commit()
                classified += 1
                log.info("Classified %s [%s]: %s", vid, c.confidence, details[vid].get("title"))
            except classify.ClassificationFailed as e:
                store.quarantine(conn, vid, "classify", str(e))
                conn.commit()
                errors += 1
                log.warning("Quarantined %s: %s", vid, e)

    # --- synthesise comments for recent videos without themes ---
    if not skip_synthesis:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SYNTH_MAX_AGE_DAYS)).isoformat()
        todo = [r["video_id"] for r in conn.execute(
            """SELECT v.video_id FROM videos v LEFT JOIN comment_themes t ON t.video_id = v.video_id
               WHERE t.video_id IS NULL AND v.published_at >= ?""", (cutoff,))]
        for vid in todo:
            video = store.get_video(conn, vid)
            try:
                comments = fetch.fetch_comments(yt, vid)  # in memory only, never stored
                themes = synthesise.synthesise_comments(ai, video, comments)
                store.save_comment_themes(conn, vid, themes)
                conn.commit()
            except fetch.QuotaExceeded as e:
                log.error("Quota exhausted during comment fetch; remaining videos retry next run: %s", e)
                errors += 1
                break
            except (fetch.YouTubeError, synthesise.SynthesisFailed) as e:
                log.warning("Synthesis skipped for %s (will retry next run): %s", vid, e)
                errors += 1
            finally:
                comments = None

    # --- report + deliver ---
    md = report.build_digest(conn, rubric, brands, overflow=result.overflow)
    message = ""
    if not no_deliver:
        try:
            out = deliver.deliver(md, store.today())
            message = f"digest: {out['file']}" + (" + Slack" if out["slack"] else "")
        except Exception as e:  # delivery failure shouldn't lose the run's data
            log.error("Delivery failed: %s", e)
            errors += 1
            message = f"delivery failed: {e}"
    store.finish_run(conn, run_id, len(result.new_videos), classified, errors,
                     status="ok" if errors == 0 else "ok_with_errors", message=message)
    log.info("Run %d complete: %d new, %d classified, %d errors", run_id,
             len(result.new_videos), classified, errors)
    return 0


def refresh_stats() -> int:
    """Discover new uploads and snapshot view/like/comment counts. YouTube only
    (a few quota units per channel); classification and synthesis wait for the
    weekly run, which picks up anything left unclassified."""
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    conn = store.connect()
    run_id = store.start_run(conn)
    try:
        yt = fetch.YouTubeClient(os.getenv("YOUTUBE_API_KEY", ""))
        result = fetch.run_fetch(conn, yt, load_brands())
    except fetch.YouTubeError as e:
        conn.rollback()
        log.error("Stats refresh failed, state left untouched: %s", e)
        store.finish_run(conn, run_id, 0, 0, 1, status="failed", message=f"stats refresh: {e}")
        return 1
    store.finish_run(conn, run_id, len(result.new_videos), 0, 0, status="ok",
                     message=f"stats refresh: {result.refreshed} videos, {yt.units_used} units")
    log.info("Stats refresh: %d new videos, %d snapshots, %d YouTube units",
             len(result.new_videos), result.refreshed, yt.units_used)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-synthesis", action="store_true")
    p.add_argument("--no-deliver", action="store_true")
    p.add_argument("--stats-only", action="store_true",
                   help="daily refresh: new uploads and view snapshots, no LLM, no digest")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(refresh_stats() if a.stats_only else run(a.skip_synthesis, a.no_deliver))


if __name__ == "__main__":
    main()
