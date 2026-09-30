"""The one place that talks to the LLM provider (OpenAI).

classify.py and synthesise.py build provider-neutral content parts and a JSON
schema; this module turns them into an OpenAI Chat Completions call with strict
structured output. Swapping provider means rewriting this file only.
"""
from __future__ import annotations

import os

import openai

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low")


class LLMError(RuntimeError):
    """Any failure that should quarantine/skip the item, never crash the run."""


def text(s: str) -> dict:
    return {"kind": "text", "text": s}


def image(b64: str, media_type: str) -> dict:
    return {"kind": "image", "data": b64, "media_type": media_type}


def _to_openai(part: dict) -> dict:
    if part["kind"] == "image":
        return {"type": "image_url",
                "image_url": {"url": f"data:{part['media_type']};base64,{part['data']}"}}
    return {"type": "text", "text": part["text"]}


def complete_json(client, system: str, parts: list[dict], schema: dict, name: str,
                  max_tokens: int = 4000) -> str:
    """Returns the raw JSON text. Raises LLMError on refusal, truncation or API failure.
    Parsing/validation is left to the caller so its retry logic stays in one place."""
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": [_to_openai(p) for p in parts]}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": name, "schema": schema, "strict": True}},
        )
    except openai.APIStatusError as e:
        raise LLMError(f"OpenAI API error {e.status_code}: {e.message}") from e
    except openai.APIConnectionError as e:
        raise LLMError(f"OpenAI connection error: {e}") from e
    choice = resp.choices[0]
    if choice.message.refusal:
        raise LLMError(f"model refused: {choice.message.refusal[:200]}")
    if choice.finish_reason == "length":
        raise LLMError("response truncated (max_completion_tokens)")
    return choice.message.content or ""
