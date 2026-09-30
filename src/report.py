"""Render the weekly digest as markdown (Slack / file). All selection logic lives in
digest.py; this module only formats. Lead with the insight; never open with a table.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import digest
from .digest import label  # noqa: F401  (re-exported for callers/tests)


def _verdict_md(c: dict) -> str:
    head = (f"**{c['verdict']}:** \"{c['title']}\" — {c['index_text']} the "
            f"{c['bucket_singular']} median, "
            + (f"{c['engagement_text']} engagement" if c["engagement"] is not None
               else "likes hidden"))
    body = []
    if c["chips"]:
        body.append(" · ".join(ch["label"] for ch in c["chips"]) +
                    (" *(low confidence)*" if c["confidence"] == "low" else ""))
    if c["theme_line"]:
        body.append(c["theme_line"])
    return "  \n".join([head] + body)  # markdown hard line breaks


def _leader_md(c: dict) -> str:
    early = f"{c['views']:,} views so far" if c["views"] is not None else ""
    if early and c["index"] is not None:
        early += f" ({c['index_text']} {c['bucket_singular']} median)"
    attrs = " · ".join(ch["label"] for ch in c["chips"]) or "not classified"
    item = f"- \"{c['title']}\" — {early}  \n  {attrs}"
    if c["theme_line"]:
        item += f"  \n  {c['theme_line']}"
    return item


def render(d: dict) -> str:
    out = [f"## Week of {d['week_start'].date():%d %b %Y}",
           f"**{d['headline']}**",
           f"{d['total_new']} new videos across {len(d['brands'])} brands."]

    for b in d["brands"]:
        mix = ", ".join(f"{n} {k}" for k, n in b["mix"])
        lines = [f"### {b['name']} — {b['new_count']} new video"
                 f"{'s' if b['new_count'] != 1 else ''} this week" + (f" ({mix})" if mix else "")]
        for c in b["verdicts"]:
            lines += [_verdict_md(c), ""]
        for attr, idx, n in b["winners"]:
            lines.append(f"**What's working for {b['name']}:** {attr} videos run at "
                         f"{idx:.1f}x their format median ({n} videos).")
        if b["shift"]:
            lines.append(f"**Shift worth noting:** {b['shift']['text']}")
        if b["leaders"]:
            lines += ["", "**Just launched** *(under 7 days — too early to judge)*"]
            if b["launched_pattern"]:
                lines.append(f"Mostly {b['launched_pattern']}.")
            lines.append("")
            lines += [_leader_md(c) for c in b["leaders"]]
            if b["launched_more"]:
                lines.append(f"- *…and {b['launched_more']} more.*")
        if b["comments_off"]:
            lines += ["", "*Comments are turned off on this channel's recent videos, "
                          "so there are no audience themes.*"]
        if b["quiet"]:
            lines.append("Quiet week — nothing new and nothing crossed the 7-day mark.")
        if not b["has_baseline"]:
            lines.append("*No baseline yet — needs videos at least 7 days old.*")
        out.append("\n".join(lines).rstrip())

    f = d["flags"]
    flags = []
    if f["low"]:
        n = len(f["low"])
        flags.append(f"{n} classification{'s' if n != 1 else ''} came back low confidence: "
                     + ", ".join(f'"{c["title"]}"' for c in f["low"]))
    if f["early_count"]:
        n = f["early_count"]
        flags.append(f"{n} video{'s' if n != 1 else ''} too recent to judge "
                     "(summarised under Just launched)")
    if f["quarantined"]:
        n = len(f["quarantined"])
        flags.append(f"{n} video{'s' if n != 1 else ''} could not be classified: "
                     + ", ".join(f'"{q["title"]}"' for q in f["quarantined"]))
    for brand, n in f["overflow"].items():
        flags.append(f"{brand} posted an unusually large batch; {n} videos deferred to next run")
    if flags:
        out.append("\n### Flagged")
        out += flags
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(out)) + "\n"


def build_digest(conn, rubric: dict, brands: list[dict], now: datetime | None = None,
                 overflow: dict[str, int] | None = None) -> str:
    return render(digest.build(conn, rubric, brands, now, overflow))
