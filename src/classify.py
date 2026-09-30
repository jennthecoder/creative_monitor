"""Claude: classify one video against the rubric.

The rubric is injected from rubric.yaml into both the prompt and the JSON schema,
so swapping the rubric changes what is measured without touching this file.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass

import anthropic
import requests

log = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "low")
CONFIDENCE = ["high", "medium", "low"]
DESCRIPTION_CHARS = 1500

SYSTEM = (
    "You are a senior creative strategist at a performance marketing agency. "
    "You classify competitor brand videos against a fixed creative rubric so that "
    "patterns can be compared week over week. Judge from the title, description, "
    "tags, duration and thumbnail. Pick exactly one allowed value per dimension. "
    "Use 'other' or 'none' when nothing fits rather than stretching a category. "
    "Set confidence to 'low' when the evidence is thin (e.g. a vague title and a "
    "generic thumbnail), 'medium' when you are inferring, 'high' when it is clear."
)


class ClassificationFailed(RuntimeError):
    pass


@dataclass
class Classification:
    values: dict[str, str]
    confidence: str
    rationale: str
    invalid: list[str]  # dimensions whose value was outside the rubric


def build_schema(rubric: dict) -> dict:
    props = {dim: {"type": "string", "enum": vals} for dim, vals in rubric["dimensions"].items()}
    props["confidence"] = {"type": "string", "enum": CONFIDENCE}
    props["rationale"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def build_prompt(video: dict, rubric: dict) -> str:
    dims = "\n".join(f"- {d}: {', '.join(v)}" for d, v in rubric["dimensions"].items())
    desc = (video.get("description") or "")[:DESCRIPTION_CHARS]
    tags = ", ".join(video.get("tags") or []) or "(none)"
    return (
        f"Rubric (allowed values per dimension):\n{dims}\n\n"
        f"Video:\n"
        f"- Brand: {video.get('brand')}\n"
        f"- Title: {video.get('title')}\n"
        f"- Duration: {video.get('duration_seconds')} seconds\n"
        f"- Tags: {tags}\n"
        f"- Description:\n{desc}\n\n"
        "The thumbnail is attached above. Return JSON with one value per dimension, "
        "plus confidence and a one-sentence rationale."
    )


def fetch_thumbnail(url: str | None, session=requests) -> dict | None:
    if not url:
        return None
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("thumbnail fetch failed for %s: %s", url, e)
        return None
    media = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if media not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        media = "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(r.content).decode()}}


def validate(raw: str, rubric: dict) -> Classification:
    """Step 1 + 2 of the plan's validation. Raises ValueError on unparseable JSON;
    out-of-rubric values are kept but force confidence to 'low' (never coerced)."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    values, invalid = {}, []
    for dim, allowed in rubric["dimensions"].items():
        v = data.get(dim)
        values[dim] = v
        if v not in allowed:
            invalid.append(dim)
    confidence = data.get("confidence") if data.get("confidence") in CONFIDENCE else "low"
    if invalid:
        log.warning("out-of-rubric values for %s: %s", invalid, {d: values[d] for d in invalid})
        confidence = "low"
    return Classification(values, confidence, str(data.get("rationale", ""))[:500], invalid)


def _call(client, content: list, schema: dict) -> str:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise ClassificationFailed("model declined to classify")
    if resp.stop_reason == "max_tokens":
        raise ClassificationFailed("response truncated at max_tokens")
    return next((b.text for b in resp.content if b.type == "text"), "")


def classify_video(client, video: dict, rubric: dict, thumbnail: dict | None = None) -> Classification:
    """One Claude call; one retry with the parse error appended. Raises
    ClassificationFailed after the second failure so the caller can quarantine."""
    schema = build_schema(rubric)
    content = ([thumbnail] if thumbnail else []) + [{"type": "text", "text": build_prompt(video, rubric)}]
    try:
        try:
            return validate(_call(client, content, schema), rubric)
        except ValueError as first:  # JSONDecodeError is a ValueError
            log.warning("classification parse failed for %s, retrying: %s",
                        video.get("video_id"), first)
            retry = content + [{"type": "text", "text":
                                f"Your previous response could not be parsed ({first}). "
                                "Return only the JSON object."}]
            try:
                return validate(_call(client, retry, schema), rubric)
            except ValueError as second:
                raise ClassificationFailed(f"unparseable after retry: {second}") from second
    except anthropic.APIStatusError as e:
        raise ClassificationFailed(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ClassificationFailed(f"connection error: {e}") from e
