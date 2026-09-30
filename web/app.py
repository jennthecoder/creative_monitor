"""Creative Monitor web app: http://127.0.0.1:8765

Read-only views over the pipeline's state, plus the rubric editor. Every page is
rendered from src/digest.py, the same builder that produces the Slack digest.

URLs are folder-style with no query strings, so scripts/build_site.py can render
every page to static HTML for GitHub Pages. With STATIC_SITE=1 the rubric page is
read-only (there's no server to save to).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from src import digest, store
from src.config import CONFIG_DIR, load_brands, load_rubric
from src.metrics import BUCKETS, channel_baseline, score_brand

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = Flask(__name__)
app.json.sort_keys = False  # rubric dimension order is meaningful
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["STATIC_SITE"] = os.getenv("STATIC_SITE") == "1"
FORMATS = ("all",) + BUCKETS
SORTS = ("newest", "best", "weakest")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _conn():
    return store.connect()


def _week_end(arg: str | None) -> datetime:
    """/week/YYYY-MM-DD/ names the last day of the digest week; default is now."""
    now = datetime.now(timezone.utc)
    if not arg:
        return now
    try:
        d = datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError:
        abort(400)
    end = datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)
    return min(end, now)


def _brand(slug: str) -> dict:
    for b in load_brands():
        if digest.slug(b["name"]) == slug:
            return b
    abort(404)


def _rubric_path() -> Path:
    return CONFIG_DIR / os.getenv("RUBRIC_FILE", "rubric.yaml")


STOPWORDS = {"the", "of", "a", "an", "and", "with", "by", "on"}


def _initials(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in STOPWORDS]
    return "".join(w[0] for w in words[:2]).upper() or name[:1].upper()


@app.context_processor
def _globals():
    conn = _conn()
    run = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return {
        "nav_brands": [{"name": b["name"], "slug": digest.slug(b["name"])} for b in load_brands()],
        "last_run": dict(run) if run else None,
        "path": request.path,
        "static_site": app.config["STATIC_SITE"],
        "initials": _initials,
    }


@app.template_filter("dt")
def _dt(value, fmt="%d %b %Y"):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.strftime(fmt).lstrip("0")


# --- pages ----------------------------------------------------------------

@app.get("/")
@app.get("/week/<week>/")
def digest_page(week=None):
    end = _week_end(week)
    conn = _conn()
    d = digest.build(conn, load_rubric(), load_brands(), end)
    now = datetime.now(timezone.utc)
    # Navigation stops at the first week with data, so the site has a finite set of pages.
    first = conn.execute("SELECT MIN(published_at) FROM videos").fetchone()[0]
    prev_end = end - timedelta(days=7)
    prev_url = (url_for("digest_page", week=prev_end.date().isoformat())
                if first and prev_end >= datetime.fromisoformat(first.replace("Z", "+00:00"))
                else None)
    is_current = (now - end) < timedelta(days=1)
    next_url = None
    if not is_current:
        nxt = end + timedelta(days=7)
        next_url = (url_for("digest_page") if nxt + timedelta(days=1) >= now
                    else url_for("digest_page", week=nxt.date().isoformat()))
    return render_template("digest.html", d=d, prev_url=prev_url, next_url=next_url,
                           is_current=is_current)


@app.get("/channel/<slug>/", defaults={"fmt": "all", "sort": "newest"})
@app.get("/channel/<slug>/<fmt>/<sort>/")
def brand_page(slug, fmt, sort):
    b = _brand(slug)
    if fmt not in FORMATS or sort not in SORTS:
        abort(404)
    fmt = None if fmt == "all" else fmt
    sort = None if sort == "newest" else sort
    conn, rubric = _conn(), load_rubric()
    now = datetime.now(timezone.utc)
    scores = score_brand(conn, b["name"], now)
    videos = digest.brand_videos(conn, b["name"])
    cards = [digest.card(v, scores, rubric, now) for v in videos]
    if fmt:
        cards = [c for c in cards if c["bucket"] == fmt]
    if sort:
        # Judged videos only: an under-7-day index is still moving.
        cards = sorted([c for c in cards if c["index"] is not None and not c["too_early"]],
                       key=lambda c: c["index"], reverse=(sort == "best"))
    weeks: dict[str, list] = {}
    for c in cards if not sort else []:
        p = datetime.fromisoformat(c["published_at"].replace("Z", "+00:00"))
        monday = (p - timedelta(days=p.weekday())).date()
        weeks.setdefault(monday.isoformat(), []).append(c)
    baselines = {k: channel_baseline(conn, b["name"], now, bucket=k) for k in BUCKETS}
    counts = {k: sum(1 for s in scores.values() if s.bucket == k) for k in BUCKETS}
    return render_template(
        "brand.html", brand=b, slug=slug, weeks=weeks, fmt=fmt, total=len(videos),
        sort=sort, sorted_cards=cards if sort else [],
        baselines={k: digest.fmt_views(int(v)) if v else None for k, v in baselines.items()},
        counts=counts, bucket_names=digest.BUCKET_NAMES,
        winners=digest.attribute_patterns(videos, scores, rubric),
        shift=digest.detect_shift(videos, rubric),
        trend=digest.publishing_trend(videos, now - timedelta(days=7)))


@app.get("/video/<video_id>/")
def video_page(video_id):
    conn, rubric = _conn(), load_rubric()
    v = store.get_video(conn, video_id)
    if not v:
        abort(404)
    now = datetime.now(timezone.utc)
    scores = score_brand(conn, v["brand"], now)
    videos = digest.brand_videos(conn, v["brand"])
    this = next(x for x in videos if x["video_id"] == video_id)
    c = digest.card(this, scores, rubric, now)
    s = scores.get(video_id)
    baseline = digest.fmt_views(int(s.baseline)) if s and s.baseline else None
    age_matched_base = any(x.age_matched for x in scores.values() if s and x.bucket == s.bucket)
    history = [dict(r) for r in conn.execute(
        "SELECT measured_at, views, likes, comment_count FROM metrics WHERE video_id = ? "
        "ORDER BY measured_at DESC", (video_id,))]
    more = [digest.card(x, scores, rubric, now) for x in videos if x["video_id"] != video_id][:12]
    q = conn.execute("SELECT reason FROM quarantine WHERE video_id = ?", (video_id,)).fetchone()
    return render_template("video.html", c=c, baseline=baseline, age_matched_base=age_matched_base, history=history, more=more,
                           quarantine=q["reason"] if q else None,
                           rubric_matches=(c["rubric_version"] in (None, rubric["version"])))


@app.get("/rubric/")
def rubric_page():
    path = _rubric_path()
    r = load_rubric(path)
    others = sorted(p.name for p in CONFIG_DIR.glob("rubric*.yaml"))
    counts = dict(_conn().execute(
        "SELECT rubric_version, COUNT(*) FROM classifications GROUP BY rubric_version").fetchall())
    return render_template("rubric.html", rubric=r, file=path.name, others=others,
                           counts=counts, saved=request.args.get("saved"))


@app.post("/rubric/")
def rubric_save():
    if app.config["STATIC_SITE"]:
        abort(405)
    try:
        dims = json.loads(request.form["dimensions"])
        clean = validate_rubric(dims)
    except (KeyError, ValueError) as e:
        current = load_rubric(_rubric_path())
        try:  # keep the user's draft on screen so nothing they typed is lost
            draft = json.loads(request.form.get("dimensions", "{}"))
            draft = {str(k): [str(x) for x in v] for k, v in draft.items()}
        except (ValueError, AttributeError, TypeError):
            draft = current["dimensions"]
        return render_template(
            "rubric.html", rubric={"version": current["version"], "dimensions": draft},
            original_dims=current["dimensions"], error=str(e), file=_rubric_path().name,
            others=sorted(p.name for p in CONFIG_DIR.glob("rubric*.yaml")), counts={}), 400
    path = _rubric_path()
    current = load_rubric(path)
    if clean == current["dimensions"]:
        return redirect(url_for("rubric_page", saved="unchanged"))
    version = bump_version(current["version"])
    write_rubric(path, version, clean)
    return redirect(url_for("rubric_page", saved=version))


def validate_rubric(dims) -> dict[str, list[str]]:
    if not isinstance(dims, dict) or not dims:
        raise ValueError("A rubric needs at least one dimension.")
    clean = {}
    for name, values in dims.items():
        name = str(name).strip().lower().replace(" ", "_")
        if not NAME_RE.match(name):
            raise ValueError(f"“{name}” isn’t a valid dimension name (lowercase letters, "
                             "digits and underscores).")
        if name in clean:
            raise ValueError(f"Dimension “{name}” appears twice.")
        vals = []
        for v in values:
            v = str(v).strip().lower().replace(" ", "_")
            if not NAME_RE.match(v):
                raise ValueError(f"“{v}” in {name} isn’t a valid value.")
            if v in vals:
                raise ValueError(f"“{v}” appears twice in {name}.")
            vals.append(v)
        if len(vals) < 2:
            raise ValueError(f"{name} needs at least two allowed values.")
        clean[name] = vals
    return clean


def bump_version(v: str) -> str:
    m = re.match(r"^(.*?)(\d+)$", v)
    return f"{m.group(1)}{int(m.group(2)) + 1}" if m else f"{v}-2"


def write_rubric(path: Path, version: str, dims: dict[str, list[str]]) -> None:
    header = [line for line in path.read_text().splitlines() if line.startswith("#")] \
        if path.exists() else []
    body = yaml.safe_dump({"version": version,
                           "dimensions": {k: {"values": v} for k, v in dims.items()}},
                          sort_keys=False, default_flow_style=None, width=88)
    path.write_text("\n".join(header + [body]))


@app.get("/api/digest")
def api_digest():
    d = digest.build(_conn(), load_rubric(), load_brands(), _week_end(request.args.get("week")))
    return jsonify(json.loads(json.dumps(d, default=str)))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
