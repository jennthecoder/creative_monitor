"""Build the weekly digest as structured data. Deterministic: reads the DB, no LLM.

Both surfaces render from this: report.py (markdown for Slack / file) and the web
app. Keeping one builder means the two can never disagree about what happened.

Because the job runs weekly, almost every video launched this week is under the
7-day age guard. So each brand section has two parts:
  - videos that *crossed* the 7-day mark this week get a performance verdict
  - videos launched this week are summarised as "just launched" with early signal
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from . import store
from .metrics import MIN_AGE_DAYS, _parse, score_brand

OUTPERFORM = 1.5
UNDERPERFORM = 0.67
MIN_ATTR_VIDEOS = 3
SHIFT_WINDOW = 5
MAX_VERDICTS = 2
EARLY_LEADERS = 3
TREND_WEEKS = 4
HEADLINE_DIMS = ("hook_type", "angle", "topic")
BUCKET_NAMES = {"short": "Shorts", "clip": "clips", "episode": "full episodes"}
BUCKET_SINGULAR = {"short": "Short", "clip": "clip", "episode": "full-episode"}

# Optional friendlier labels for known rubrics; unknown dimensions fall back to the
# bare value, so a swapped rubric still renders sensibly.
SUFFIX = {"hook_type": "hook", "angle": "angle", "offer": "offer",
          "production_level": "production", "thumbnail_treatment": "thumbnail",
          "topic": "topic", "guest_type": "guest", "title_style": "title",
          "emotional_tone": "tone"}
SPECIAL = {("offer", "none"): "no offer", ("format", "ugc"): "UGC",
           ("guest_type", "no_guest"): "no guest"}


# --- text helpers ---------------------------------------------------------

def label(dim: str, value: str) -> str:
    """Value with its dimension suffix, for prose: 'provocative claim hook'."""
    v = (value or "?").replace("_", " ")
    if (dim, value) in SPECIAL:
        return SPECIAL[(dim, value)]
    return f"{v} {SUFFIX[dim]}" if dim in SUFFIX else v


def dim_name(dim: str) -> str:
    """'hook_type' -> 'Hook type', for chip captions."""
    return dim.replace("_", " ").capitalize()


def value_name(dim: str, value: str) -> str:
    if (dim, value) in SPECIAL:
        return SPECIAL[(dim, value)]
    return (value or "?").replace("_", " ")


def attr_line(values: dict, rubric: dict) -> str:
    return " · ".join(label(d, values.get(d)) for d in rubric["dimensions"] if values.get(d))


def _join(items: list[str]) -> str:
    items = [i.rstrip(".").strip() for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def themes_line(themes: dict | None) -> str:
    if not themes:
        return ""
    parts = []
    if themes.get("questions"):
        parts.append("Viewers keep asking about " + _join(themes["questions"][:2]))
    if themes.get("objections"):
        parts.append("pushback on " + _join(themes["objections"][:2]))
    if themes.get("praise"):
        parts.append("praise for " + _join(themes["praise"][:2]))
    if not parts and themes.get("recurring_themes"):
        parts.append("Recurring: " + _join(themes["recurring_themes"][:2]))
    s = "; ".join(parts)
    return (s[0].upper() + s[1:] + ".") if s else ""


def fmt_index(idx: float | None) -> str:
    if idx is None:
        return "—"
    return f"{idx:.2f}x" if idx < 0.1 else f"{idx:.1f}x"


def fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def fmt_views(n: int | None) -> str:
    """YouTube-style compact count: 1.2M, 48K, 950."""
    if n is None:
        return "—"
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suf if v < 10 else f"{int(v)}{suf}"
    return str(n)


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_ago(published_at: str, now: datetime) -> str:
    days = (now - _parse(published_at)).days
    if days < 1:
        hours = max(1, int((now - _parse(published_at)).total_seconds() // 3600))
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 30:
        w = days // 7
        return f"{w} week{'s' if w != 1 else ''} ago"
    mo = days // 30
    return f"{mo} month{'s' if mo != 1 else ''} ago"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def perf_class(idx: float | None) -> str | None:
    """The only four states the UI knows. Only 'out' gets the accent colour."""
    if idx is None:
        return None
    if idx >= OUTPERFORM:
        return "out"
    if idx >= 1.0:
        return "above"
    if idx > UNDERPERFORM:
        return "below"
    return "under"


def bar_position(idx: float | None) -> float | None:
    """Log-scale position 0..1 with the 1.0x baseline at 0.5 (0.1x .. 10x)."""
    if idx is None or idx <= 0:
        return None
    return min(1.0, max(0.0, (math.log10(idx) + 1) / 2))


# --- data access ----------------------------------------------------------

def brand_videos(conn, brand: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM videos WHERE brand = ? ORDER BY published_at DESC", (brand,))
    out = []
    for r in rows:
        d = dict(r)
        d["cls"] = store.get_classification(conn, d["video_id"])
        d["themes"] = store.get_comment_themes(conn, d["video_id"])
        d["has_themes_row"] = store.has_comment_themes(conn, d["video_id"])
        out.append(d)
    return out


def card(v: dict, scores: dict, rubric: dict, now: datetime) -> dict:
    """Everything a creative card needs, pre-formatted."""
    s = scores.get(v["video_id"])
    idx = s.view_index if s else None
    cls = v.get("cls")
    chips = []
    if cls:
        chips = [{"key": d, "dim": dim_name(d), "value": value_name(d, cls["values"].get(d)),
                  "label": label(d, cls["values"].get(d))}
                 for d in rubric["dimensions"] if cls["values"].get(d)]
    return {
        "video_id": v["video_id"],
        "title": v.get("title") or "(untitled)",
        "brand": v["brand"],
        "brand_slug": slug(v["brand"]),
        "published_at": v["published_at"],
        "ago": fmt_ago(v["published_at"], now),
        "duration": fmt_duration(v.get("duration_seconds")),
        "bucket": s.bucket if s else None,
        "bucket_singular": BUCKET_SINGULAR.get(s.bucket) if s else None,
        "views": s.views if s else None,
        "views_text": fmt_views(s.views if s else None),
        "index": idx,
        "index_text": fmt_index(idx),
        "perf": perf_class(idx),
        "bar": bar_position(idx),
        "engagement": s.engagement_rate if s else None,
        "engagement_text": (fmt_pct(s.engagement_rate) if s and s.engagement_rate is not None
                            else "likes hidden"),
        "too_early": bool(s and s.too_early),
        "chips": chips,
        "confidence": cls["confidence"] if cls else None,
        "rationale": cls["rationale"] if cls else None,
        "rubric_version": cls["rubric_version"] if cls else None,
        "classified": bool(cls),
        "themes": v.get("themes"),
        "theme_line": themes_line(v.get("themes")),
        "comments_off": v.get("has_themes_row") and v.get("themes") is None,
        "thumb": f"https://i.ytimg.com/vi/{v['video_id']}/hqdefault.jpg",
        "thumb_large": f"https://i.ytimg.com/vi/{v['video_id']}/maxresdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={v['video_id']}",
    }


# --- analysis -------------------------------------------------------------

def attribute_winners(videos: list[dict], scores: dict, rubric: dict) -> list[tuple[str, float, int]]:
    """Median view index per attribute value, over judgeable, confidently-classified
    videos. Low-confidence classifications are excluded, not silently counted."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for v in videos:
        s, c = scores.get(v["video_id"]), v["cls"]
        if not s or s.too_early or s.view_index is None or not c or c["confidence"] == "low":
            continue
        for dim in rubric["dimensions"]:
            val = c["values"].get(dim)
            if val and val not in ("other",):
                buckets.setdefault((dim, val), []).append(s.view_index)
    ranked = [(label(d, v), median(xs), len(xs)) for (d, v), xs in buckets.items()
              if len(xs) >= MIN_ATTR_VIDEOS]
    return sorted([r for r in ranked if r[1] >= 1.2], key=lambda r: -r[1])[:2]


def dominant_attributes(videos: list[dict], rubric: dict, share: float = 0.4) -> str | None:
    """'curiosity gap hook (9 of 19), psychology mindset topic (11 of 19)' — values
    held by at least `share` of this week's confidently-classified launches."""
    cls = [v["cls"]["values"] for v in videos if v["cls"] and v["cls"]["confidence"] != "low"]
    if len(cls) < 3:
        return None
    found = []
    for dim in rubric["dimensions"]:
        val, n = Counter(c.get(dim) for c in cls).most_common(1)[0]
        if val not in (None, "other") and n / len(cls) >= share:
            found.append((n, f"{label(dim, val)} ({n} of {len(cls)})"))
    return ", ".join(t for _, t in sorted(found, reverse=True)[:3]) or None


def detect_shift(videos: list[dict], rubric: dict) -> dict | None:
    """Compare the most common value in the last N classified videos vs the N before."""
    classified = [v for v in videos if v["cls"] and v["cls"]["confidence"] != "low"]
    recent, prior = classified[:SHIFT_WINDOW], classified[SHIFT_WINDOW:SHIFT_WINDOW * 2]
    if len(recent) < SHIFT_WINDOW or len(prior) < SHIFT_WINDOW:
        return None
    best = None
    for dim in rubric["dimensions"]:
        rc = Counter(v["cls"]["values"].get(dim) for v in recent)
        pc = Counter(v["cls"]["values"].get(dim) for v in prior)
        val, n_recent = rc.most_common(1)[0]
        delta = n_recent - pc.get(val, 0)
        if val not in ("other", None) and delta >= 2 and (not best or delta > best[0]):
            best = (delta, dim, val, n_recent, pc.get(val, 0))
    if not best:
        return None
    _, dim, val, nr, np_ = best
    return {"label": label(dim, val), "recent": nr, "prior": np_, "window": SHIFT_WINDOW,
            "text": (f"{nr} of the last {SHIFT_WINDOW} videos are {label(dim, val)}, "
                     f"up from {np_} of {SHIFT_WINDOW} before that.")}


def publishing_trend(videos: list[dict], week_start: datetime) -> dict:
    """This week's upload count vs the average of the previous TREND_WEEKS weeks
    (only weeks the data actually covers)."""
    pubs = [_parse(v["published_at"]) for v in videos]
    this = sum(week_start <= p < week_start + timedelta(days=7) for p in pubs)
    earliest = min(pubs) if pubs else week_start
    prior = []
    for k in range(1, TREND_WEEKS + 1):
        lo = week_start - timedelta(days=7 * k)
        if lo < earliest:
            break
        prior.append(sum(lo <= p < lo + timedelta(days=7) for p in pubs))
    if not prior:
        return {"dir": None, "this": this, "usual": None}
    usual = mean(prior)
    d = "up" if this >= usual * 1.25 else "down" if this <= usual * 0.75 else "flat"
    return {"dir": d, "this": this, "usual": round(usual, 1)}


def headline(brand_sections: list[dict]) -> str:
    """The single most notable change this week, as a sentence. Deterministic:
    a big outperformer beats a pattern shift, which beats a modest outperformer."""
    outs = [(c["index"], b, c) for b in brand_sections for c in b["verdicts"] if c["perf"] == "out"]
    outs.sort(key=lambda x: -x[0])
    shifts = [b for b in brand_sections if b["shift"]]

    def out_sentence(idx, b, c):
        # Describe it by its creative levers (hook / angle / topic) when the rubric has them.
        levers = [ch for ch in c["chips"] if ch["key"] in HEADLINE_DIMS] or c["chips"]
        tail = (f", pairing a {levers[0]['label']} with a {levers[1]['label']}"
                if len(levers) >= 2 else "")
        return (f"{b['name']}’s “{c['title']}” is running at {c['index_text']} its "
                f"{c['bucket_singular']} median{tail}.")

    if outs and outs[0][0] >= 3:
        return out_sentence(*outs[0])
    if shifts:
        b = shifts[0]
        s = b["shift"]
        return (f"{b['name']} is leaning into {s['label']}: {s['recent']} of its last "
                f"{s['window']} videos, up from {s['prior']} before.")
    if outs:
        return out_sentence(*outs[0])
    total = sum(b["new_count"] for b in brand_sections)
    return (f"A steady week: {total} new videos, and nothing broke far from its channel’s "
            f"baseline.")


# --- the builder ----------------------------------------------------------

def build(conn, rubric: dict, brands: list[dict], now: datetime | None = None,
          overflow: dict[str, int] | None = None) -> dict:
    """`now` is the end of the digest week. Views are the latest measurement, so a
    past week is shown with today's counts (noted in the UI)."""
    now = now or datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    judge_lo = now - timedelta(days=MIN_AGE_DAYS + 7)
    judge_hi = now - timedelta(days=MIN_AGE_DAYS)

    sections, flagged_low, early_count = [], [], 0
    for b in brands:
        name = b["name"]
        videos = [v for v in brand_videos(conn, name) if _parse(v["published_at"]) < now]
        scores = score_brand(conn, name, now)
        new = [v for v in videos if _parse(v["published_at"]) >= week_start]
        judgeable = [v for v in videos if judge_lo <= _parse(v["published_at"]) < judge_hi]

        def _idx(v):
            s = scores.get(v["video_id"])
            return s.view_index if s and s.view_index is not None else 0

        ranked = [v for v in sorted(judgeable, key=_idx, reverse=True)
                  if scores.get(v["video_id"]) and scores[v["video_id"]].view_index is not None]
        outs = [v for v in ranked if _idx(v) >= OUTPERFORM][:MAX_VERDICTS]
        unders = [v for v in reversed(ranked) if _idx(v) <= UNDERPERFORM][:MAX_VERDICTS]
        # Outperformers and underperformers are the insight; "on par" only if nothing else.
        notable = outs + unders or ranked[:1]
        verdicts = [card(v, scores, rubric, now) for v in notable]
        for c, v in zip(verdicts, notable):
            c["verdict"] = ("Outperforming" if v in outs else
                            "Underperforming" if v in unders else "On par")

        leaders = sorted(new, key=_idx, reverse=True)[:EARLY_LEADERS]
        buckets = Counter(scores[v["video_id"]].bucket for v in new if v["video_id"] in scores)
        themed = [v for v in new + judgeable if v["has_themes_row"]]
        early_count += len(new)
        flagged_low += [card(v, scores, rubric, now) for v in new + judgeable
                        if v["cls"] and v["cls"]["confidence"] == "low"]

        sections.append({
            "name": name,
            "slug": slug(name),
            "new_count": len(new),
            "mix": [(BUCKET_NAMES[k], n) for k, n in sorted(buckets.items(), reverse=True)],
            "trend": publishing_trend(videos, week_start),
            "verdicts": verdicts,
            "winners": attribute_winners(videos, scores, rubric),
            "shift": detect_shift(videos, rubric),
            "launched_pattern": dominant_attributes(new, rubric),
            "leaders": [card(v, scores, rubric, now) for v in leaders],
            "launched_more": max(0, len(new) - len(leaders)),
            "comments_off": bool(themed) and all(v["themes"] is None for v in themed),
            "has_baseline": any(s.baseline for s in scores.values()),
            "quiet": not new and not notable,
        })

    quarantined = [dict(r) for r in conn.execute(
        """SELECT v.video_id, v.title, q.reason FROM quarantine q
           JOIN videos v ON v.video_id = q.video_id
           WHERE q.quarantined_at >= ? AND q.quarantined_at < ?""",
        (week_start.isoformat(), (now + timedelta(days=1)).isoformat()))]
    run = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return {
        "week_start": week_start,
        "week_end": now,
        "total_new": sum(s["new_count"] for s in sections),
        "headline": headline(sections),
        "brands": sections,
        "flags": {"low": flagged_low, "early_count": early_count,
                  "quarantined": quarantined, "overflow": overflow or {}},
        "last_run": dict(run) if run else None,
    }
