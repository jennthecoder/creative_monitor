"""Claude: turn the top comments on a video into audience themes.

Privacy: comments arrive as plain text (fetch.py already dropped author names/IDs),
live only in memory for the duration of this call, and are never persisted. Only
the synthesised themes are stored.
"""
from __future__ import annotations

import json
import logging

import anthropic

from .classify import EFFORT, MODEL

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 500
FIELDS = ["recurring_themes", "objections", "questions", "praise"]

SYSTEM = (
    "You are a creative strategist reading YouTube comments on a competitor's video. "
    "Summarise what the audience is saying so a strategist can act on it. Report "
    "patterns, not individual comments: only include a point if several comments "
    "share it. Paraphrase; never quote a comment verbatim and never include names, "
    "handles or anything that identifies a commenter. Each item is one short phrase. "
    "Phrase every item as a short noun phrase that reads after 'viewers keep asking "
    "about …' or 'praise for …' (e.g. 'sizing', 'colour fading after washing'), not "
    "as a full sentence or question. Use an empty list when nothing recurs."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "recurring_themes": {"type": "array", "items": {"type": "string"}},
        "objections": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "praise": {"type": "array", "items": {"type": "string"}},
        "overall_sentiment": {"type": "string", "enum": ["positive", "mixed", "negative"]},
    },
    "required": FIELDS + ["overall_sentiment"],
    "additionalProperties": False,
}


class SynthesisFailed(RuntimeError):
    pass


def synthesise_comments(client, video: dict, comments: list[str] | None) -> dict | None:
    """Returns themes dict, or None when comments are disabled / there are none."""
    if not comments:
        return None
    body = "\n".join(f"- {c[:MAX_COMMENT_CHARS].replace(chr(10), ' ')}" for c in comments if c.strip())
    prompt = (f"Video: \"{video.get('title')}\" by {video.get('brand')}\n\n"
              f"Top {len(comments)} comments by relevance:\n{body}")
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        raise SynthesisFailed(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise SynthesisFailed(f"connection error: {e}") from e
    if resp.stop_reason in {"refusal", "max_tokens"}:
        raise SynthesisFailed(f"stop_reason={resp.stop_reason}")
    try:
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (StopIteration, ValueError) as e:
        raise SynthesisFailed(f"unparseable response: {e}") from e
    themes = {k: [str(x)[:200] for x in data.get(k, [])][:5] for k in FIELDS}
    themes["overall_sentiment"] = data.get("overall_sentiment", "mixed")
    themes["comments_analysed"] = len(comments)
    return themes
