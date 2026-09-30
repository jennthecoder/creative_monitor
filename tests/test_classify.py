import json
from types import SimpleNamespace

import pytest

from src import classify
from src.config import load_rubric

RUBRIC = load_rubric()
VIDEO = {"video_id": "v1", "brand": "Brand A", "title": "Why our shoes last 5 years",
         "description": "Tested on 1000 miles", "tags": ["shoes"], "duration_seconds": 45}

GOOD = {"hook_type": "bold_claim", "format": "talking_head", "angle": "quality_durability",
        "offer": "none", "production_level": "mid", "thumbnail_treatment": "face",
        "confidence": "high", "rationale": "Durability claim delivered to camera."}


class FakeClient:
    """Returns queued raw text responses from beta.messages.create."""

    def __init__(self, *responses, stop_reason="end_turn"):
        self.responses, self.calls = list(responses), []
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.responses.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=text)])


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
    assert "could not be parsed" in fc.calls[1]["messages"][0]["content"][-1]["text"]


def test_malformed_twice_raises_for_quarantine():
    with pytest.raises(classify.ClassificationFailed):
        classify.classify_video(FakeClient("nope", "still nope"), VIDEO, RUBRIC)


def test_out_of_rubric_value_forces_low_confidence_not_coerced():
    bad = dict(GOOD, format="podcast")
    c = classify.classify_video(FakeClient(json.dumps(bad)), VIDEO, RUBRIC)
    assert c.confidence == "low" and c.invalid == ["format"] and c.values["format"] == "podcast"


def test_refusal_raises():
    with pytest.raises(classify.ClassificationFailed):
        classify.classify_video(FakeClient(json.dumps(GOOD), stop_reason="refusal"), VIDEO, RUBRIC)


def test_thumbnail_is_sent_first():
    fc = FakeClient(json.dumps(GOOD))
    thumb = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AA=="}}
    classify.classify_video(fc, VIDEO, RUBRIC, thumbnail=thumb)
    assert fc.calls[0]["messages"][0]["content"][0]["type"] == "image"
