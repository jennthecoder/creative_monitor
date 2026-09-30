"""SQLite state. All reads/writes go through here so reruns stay idempotent."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  brand TEXT NOT NULL,
  uploads_playlist_id TEXT NOT NULL,
  cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  brand TEXT NOT NULL,
  published_at TEXT NOT NULL,
  title TEXT,
  duration_seconds INTEGER,
  first_seen_at TEXT NOT NULL
);

-- Dimension values are stored as JSON keyed by rubric dimension, so swapping
-- rubric.yaml never requires a schema migration.
CREATE TABLE IF NOT EXISTS classifications (
  video_id TEXT PRIMARY KEY REFERENCES videos(video_id),
  values_json TEXT NOT NULL,
  confidence TEXT,
  rationale TEXT,
  rubric_version TEXT NOT NULL,
  classified_at TEXT NOT NULL
);

-- measured_at is a UTC date, so same-day reruns overwrite rather than duplicate.
CREATE TABLE IF NOT EXISTS metrics (
  video_id TEXT REFERENCES videos(video_id),
  measured_at TEXT NOT NULL,
  views INTEGER, likes INTEGER, comment_count INTEGER,
  PRIMARY KEY (video_id, measured_at)
);

CREATE TABLE IF NOT EXISTS comment_themes (
  video_id TEXT PRIMARY KEY REFERENCES videos(video_id),
  themes_json TEXT,          -- synthesised themes only, no raw comments; NULL = comments disabled
  synthesised_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  video_id TEXT PRIMARY KEY REFERENCES videos(video_id),
  stage TEXT NOT NULL,
  reason TEXT,
  quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, completed_at TEXT,
  videos_found INTEGER, videos_classified INTEGER, errors INTEGER,
  status TEXT, message TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- channels -------------------------------------------------------------

def get_uploads_playlist(conn, channel_id: str) -> str | None:
    row = conn.execute(
        "SELECT uploads_playlist_id FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return row["uploads_playlist_id"] if row else None


def cache_uploads_playlist(conn, channel_id: str, brand: str, playlist_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO channels VALUES (?, ?, ?, ?)",
        (channel_id, brand, playlist_id, now_iso()),
    )


# --- videos ---------------------------------------------------------------

def known_video_ids(conn, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT video_id FROM videos WHERE video_id IN ({marks})", ids)
    return {r["video_id"] for r in rows}


def insert_video(conn, video: dict) -> bool:
    """Insert a video; returns False if it already existed (rerun-safe)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO videos
           (video_id, brand, published_at, title, duration_seconds, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            video["video_id"], video["brand"], video["published_at"],
            video.get("title"), video.get("duration_seconds"), now_iso(),
        ),
    )
    return cur.rowcount == 1


def get_video(conn, video_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def latest_published_at(conn, brand: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(published_at) AS p FROM videos WHERE brand = ?", (brand,)
    ).fetchone()
    return row["p"]


def recent_video_ids(conn, brand: str, limit: int = 20) -> list[str]:
    rows = conn.execute(
        "SELECT video_id FROM videos WHERE brand = ? ORDER BY published_at DESC LIMIT ?",
        (brand, limit),
    )
    return [r["video_id"] for r in rows]


# --- classifications ------------------------------------------------------

def save_classification(conn, video_id: str, values: dict, confidence: str,
                        rationale: str, rubric_version: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO classifications VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, json.dumps(values), confidence, rationale, rubric_version, now_iso()),
    )


def get_classification(conn, video_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM classifications WHERE video_id = ?", (video_id,)
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["values"] = json.loads(out.pop("values_json"))
    return out


def unclassified_video_ids(conn) -> list[str]:
    rows = conn.execute(
        """SELECT v.video_id FROM videos v
           LEFT JOIN classifications c ON c.video_id = v.video_id
           LEFT JOIN quarantine q ON q.video_id = v.video_id AND q.stage = 'classify'
           WHERE c.video_id IS NULL AND q.video_id IS NULL"""
    )
    return [r["video_id"] for r in rows]


def quarantine(conn, video_id: str, stage: str, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO quarantine VALUES (?, ?, ?, ?)",
        (video_id, stage, reason[:1000], now_iso()),
    )


# --- metrics --------------------------------------------------------------

def save_metrics(conn, video_id: str, views, likes, comment_count,
                 measured_at: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?)",
        (video_id, measured_at or today(), views, likes, comment_count),
    )


def latest_metrics(conn, video_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM metrics WHERE video_id = ? ORDER BY measured_at DESC LIMIT 1",
        (video_id,),
    ).fetchone()
    return dict(row) if row else None


# --- comment themes -------------------------------------------------------

def save_comment_themes(conn, video_id: str, themes: dict | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO comment_themes VALUES (?, ?, ?)",
        (video_id, json.dumps(themes) if themes is not None else None, now_iso()),
    )


def get_comment_themes(conn, video_id: str) -> dict | None:
    row = conn.execute(
        "SELECT themes_json FROM comment_themes WHERE video_id = ?", (video_id,)
    ).fetchone()
    if not row or row["themes_json"] is None:
        return None
    return json.loads(row["themes_json"])


def has_comment_themes(conn, video_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM comment_themes WHERE video_id = ?", (video_id,)
    ).fetchone() is not None


# --- runs -----------------------------------------------------------------

def start_run(conn) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, found: int, classified: int, errors: int,
               status: str = "ok", message: str = "") -> None:
    conn.execute(
        """UPDATE runs SET completed_at = ?, videos_found = ?, videos_classified = ?,
           errors = ?, status = ?, message = ? WHERE run_id = ?""",
        (now_iso(), found, classified, errors, status, message, run_id),
    )
    conn.commit()
