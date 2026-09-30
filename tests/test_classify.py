import json
from types import SimpleNamespace

import pytest

from src import classify, llm
from src.config import load_rubric

RUBRIC = load_rubric()
VIDEO = {"video_id": "v1", "brand": "Brand A", "title": "Why our shoes last 5 years",
         "description": "Tested on 1000 miles", "tags": ["shoes"], "duration_seconds": 45}

GOOD = {"hook_type": "bold_claim", "format": "talking_head", "angle": "quality_durability",
        "offer": "none", "production_level": "mid", "thumbnail_treatment": "face",
        "confidence": "high", "rationale": "Durability claim delivered to camera."}


class FakeClient:
    """Mimics openai.OpenAI().chat.completions.create with queued raw text responses."""

    def __init__(self, *responses, refusal=None, finish_reason="stop"):
        self.responses, self.calls = list(responses), []
        self.refusal, self.finish_reason = refusal, finish_reason
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.responses.pop(0) if self.responses else ""
        msg = SimpleNamespace(content=text, refusal=self.refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish_reason)])


def test_schema_built_from_rubric():
    s = classify.build_schema(RUBRIC)
    assert s["properties"]["offer"]["enum"] == RUBRIC["dimensions"]["offer"]
    assert set(s["required"]) == set(RUBRIC["dimensions"]) | {"confidence", "rationale"}


def test_swapping_rubric_changes_schema_and_prompt():
    alt = {"version": "pet-1", "dimensions": {"pet_shown": ["dog", "cat", "none"]}}
    assert list(classify.build_schema(alt)["properties"]) == ["pet_shown", "confidence", "rationale"]
    assert "pet_shown: dog, cat, none" in classify.build_prompt(VIDEO, alt)


def test_good_response():
    c = classify.classify_video(FakeClient(json.dumps(GOOD)), VIDEO, RUBRIC)
    assert c.values["angle"] == "quality_durability" and c.confidence == "high" and not c.invalid


def test_malformed_then_good_retries_once():
    fc = FakeClient("not json {", json.dumps(GOOD))
    c = classify.classify_video(fc, VIDEO, RUBRIC)
    assert c.confidence == "high" and len(fc.calls) == 2
    assert "could not be parsed" in fc.calls[1]["messages"][1]["content"][-1]["text"]


def test_malformed_twice_raises_for_quarantine():
    with pytest.raises(classify.ClassificationFailed):
        classify.classify_video(FakeClient("nope", "still nope"), VIDEO, RUBRIC)


def test_out_of_rubric_value_forces_low_confidence_not_coerced():
    bad = dict(GOOD, format="podcast")
    c = classify.classify_video(FakeClient(json.dumps(bad)), VIDEO, RUBRIC)
    assert c.confidence == "low" and c.invalid == ["format"] and c.values["format"] == "podcast"


def test_refusal_raises():
    with pytest.raises(classify.ClassificationFailed):
        classify.classify_video(FakeClient(json.dumps(GOOD), refusal="no"), VIDEO, RUBRIC)


def test_truncation_raises():
    with pytest.raises(classify.ClassificationFailed):
        classify.classify_video(FakeClient("{", finish_reason="length"), VIDEO, RUBRIC)


def test_thumbnail_is_sent_first():
    fc = FakeClient(json.dumps(GOOD))
    thumb = llm.image("AA==", "image/jpeg")
    classify.classify_video(fc, VIDEO, RUBRIC, thumbnail=thumb)
    first = fc.calls[0]["messages"][1]["content"][0]
    assert first == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}
    assert fc.calls[0]["response_format"]["json_schema"]["strict"] is True


def test_podcast_rubric_loads_and_builds_schema():
    from pathlib import Path
    r = load_rubric(Path(__file__).parent.parent / "config" / "rubric.podcast.yaml")
    assert r["version"] == "podcast-1" and "guest_type" in classify.build_schema(r)["properties"]
