"""Deliver the digest: always write reports/digest-<date>.md, and post to Slack
if SLACK_WEBHOOK_URL is set. The system works with no Slack setup."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)
REPORTS = Path(__file__).resolve().parent.parent / "reports"
SLACK_LIMIT = 35_000


def to_slack_mrkdwn(md: str) -> str:
    """Slack uses *bold* and _italic_, and has no headings."""
    out = re.sub(r"(?<![*\w])\*(?!\*)([^*\n]+?)\*(?!\*)", r"_\1_", md)   # *italic* -> _italic_
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", out)                        # **bold** -> *bold*
    out = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", out, flags=re.M)          # ## Heading -> *Heading*
    return out.replace("  \n", "\n")


def write_file(md: str, date: str, directory: Path | None = None) -> Path:
    directory = directory or REPORTS
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"digest-{date}.md"
    path.write_text(md)
    return path


def post_slack(md: str, webhook: str, session=requests) -> None:
    text = to_slack_mrkdwn(md)
    chunks = [text[i:i + SLACK_LIMIT] for i in range(0, len(text), SLACK_LIMIT)] or [""]
    for chunk in chunks:
        r = session.post(webhook, json={"text": chunk}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Slack webhook returned {r.status_code}: {r.text[:200]}")


def deliver(md: str, date: str) -> dict:
    path = write_file(md, date)
    result = {"file": str(path), "slack": False}
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if webhook:
        post_slack(md, webhook)
        result["slack"] = True
    else:
        log.info("SLACK_WEBHOOK_URL not set; digest written to %s only", path)
    return result
