"""Assemble the weekly markdown digest. Deterministic: reads the DB, no LLM.

Lead with the insight; attributes support it. Never open with a data table.

Because the job runs weekly, almost every video launched this week is under the
7-day age guard. So each brand section has two parts:
  - videos that *crossed* the 7-day mark this week get a performance verdict
  - videos launched this week are listed as "just launched" with early signal only
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from . import store
from .metrics import MIN_AGE_DAYS, _parse, score_brand

OUTPERFORM = 1.5
UNDERPERFORM = 0.67
MIN_ATTR_VIDEOS = 2
SHIFT_WINDOW = 5

# Optional friendlier labels for the default rubric; unknown dimensions fall back
# to the bare value, so a swapped rubric still renders sensibly.
SUFFIX = {"hook_type": "hook", "angle": "angle", "offer": "offer",
          "production_level": "production", "thumbnail_treatment": "thumbnail"}


def label(dim: str, value: str) -> str:
    v = (value or "?").replace("_", " ")
    if dim == "offer" and value == "none":
        return "no offer"
    if dim == "format" and value == "ugc":
        return "UGC"
    return f"{v} {SUFFIX[dim]}" if dim in SUFFIX else v


def attr_line(values: dict, rubric: dict) -> str:
    return " · ".join(label(d, values.get(d)) for d in rubric["dimensions"] if values.get(d))


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


def _join(items: list[str]) -> str:
    items = [i.rstrip(".").strip() for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _pct(x):
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def _brand_videos(conn, brand: str) -> list[dict]:
    rows = conn.execute(
        """SELECT v.*, c.values_json IS NOT NULL AS classified
           FROM videos v LEFT JOIN classifications c ON c.video_id = v.video_id
           WHERE v.brand = ? ORDER BY v.published_at DESC""", (brand,))
    out = []
    for r in rows:
        d = dict(r)
        d["cls"] = store.get_classification(conn, d["video_id"])
        d["themes"] = store.get_comment_themes(conn, d["video_id"])
        out.append(d)
    return out


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


def detect_shift(videos: list[dict], rubric: dict) -> str | None:
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
    return (f"{nr} of the last {SHIFT_WINDOW} videos are {label(dim, val)}, "
            f"up from {np_} of {SHIFT_WINDOW} before that.")


def build_digest(conn, rubric: dict, brands: list[dict], now: datetime | None = None,
                 overflow: dict[str, int] | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    judge_lo = now - timedelta(days=MIN_AGE_DAYS + 7)
    judge_hi = now - timedelta(days=MIN_AGE_DAYS)

    out = [f"## Week of {week_ago.date():%d %b %Y}"]
    flagged_low, flagged_early, total_new = [], [], 0
    sections = []

    for b in brands:
        name = b["name"]
        videos = _brand_videos(conn, name)
        scores = score_brand(conn, name, now)
        new = [v for v in videos if _parse(v["published_at"]) >= week_ago]
        judgeable = [v for v in videos if judge_lo <= _parse(v["published_at"]) < judge_hi]
        total_new += len(new)

        lines = [f"### {name} — {len(new)} new video{'s' if len(new) != 1 else ''} this week"]
        base = next((s.baseline for s in scores.values() if s.baseline), None)

        verdicts = []
        def _idx(v):
            s = scores.get(v["video_id"])
            return s.view_index if s and s.view_index is not None else 0

        for v in sorted(judgeable, key=_idx, reverse=True):
            s = scores.get(v["video_id"])
            if not s or s.view_index is None:
                continue
            if s.view_index >= OUTPERFORM:
                tag = "Outperforming"
            elif s.view_index <= UNDERPERFORM:
                tag = "Underperforming"
            else:
                tag = "On par"
            head = (f"**{tag}:** \"{v['title']}\" — {s.view_index:.1f}x channel median, "
                    f"{_pct(s.engagement_rate)} engagement")
            body = []
            if v["cls"]:
                body.append(attr_line(v["cls"]["values"], rubric) +
                            (" *(low confidence)*" if v["cls"]["confidence"] == "low" else ""))
            if tl := themes_line(v["themes"]):
                body.append(tl)
            verdicts.append((tag, head, body))
        # Outperformers and underperformers are the insight; "on par" only if nothing else.
        notable = [x for x in verdicts if x[0] != "On par"] or verdicts[:1]
        for _, head, body in notable:
            lines.append("  \n".join([head] + body))  # markdown hard line breaks
            lines.append("")

        for attr, idx, n in attribute_winners(videos, scores, rubric):
            lines.append(f"**What's working for {name}:** {attr} videos run at "
                         f"{idx:.1f}x the channel median ({n} videos).")
        if shift := detect_shift(videos, rubric):
            lines.append(f"**Shift worth noting:** {shift}")

        if new:
            lines.append("")
            lines.append("**Just launched** *(under 7 days — too early to judge)*")
            lines.append("")
            for v in new:
                s = scores.get(v["video_id"])
                attrs = attr_line(v["cls"]["values"], rubric) if v["cls"] else "not classified"
                early = f"{s.views:,} views so far" if s and s.views is not None else ""
                item = f"- \"{v['title']}\" — {attrs}" + (f" · {early}" if early else "")
                if tl := themes_line(v["themes"]):
                    item += f"  \n  {tl}"
                lines.append(item)
                flagged_early.append(v["title"])

        if not new and not notable:
            lines.append("Quiet week — nothing new and nothing crossed the 7-day mark.")
        if base is None:
            lines.append("*No baseline yet — needs videos at least 7 days old.*")

        for v in new + judgeable:
            if v["cls"] and v["cls"]["confidence"] == "low":
                flagged_low.append(v["title"])
        sections.append("\n".join(lines).rstrip())

    out.append(f"{total_new} new videos across {len(brands)} brands.\n")
    out += sections

    quarantined = conn.execute(
        """SELECT v.title, q.reason FROM quarantine q JOIN videos v ON v.video_id = q.video_id
           WHERE q.quarantined_at >= ?""", (week_ago.isoformat(),)).fetchall()
    flags = []
    if flagged_low:
        flags.append(f"{len(flagged_low)} classification{'s' if len(flagged_low) != 1 else ''} "
                     f"came back low confidence: " + ", ".join(f'"{t}"' for t in flagged_low))
    if flagged_early:
        flags.append(f"{len(flagged_early)} video{'s' if len(flagged_early) != 1 else ''} "
                     "too recent to judge (listed under Just launched)")
    if quarantined:
        flags.append(f"{len(quarantined)} video{'s' if len(quarantined) != 1 else ''} could not be "
                     "classified: " + ", ".join(f'"{r["title"]}"' for r in quarantined))
    for brand, n in (overflow or {}).items():
        flags.append(f"{brand} posted an unusually large batch; {n} videos deferred to next run")
    if flags:
        out.append("\n### Flagged")
        out += flags
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(out)) + "\n"
