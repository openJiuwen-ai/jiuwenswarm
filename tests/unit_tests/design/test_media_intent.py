# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.designer import model_tools
from jiuwenswarm.server.runtime.designer.media_intent import (
    MediaIntent,
    extract_media_intent,
    media_settings_from_intent,
    regex_media_intent,
)
from jiuwenswarm.agents.harness.common.tools.gen_toolkits import MODELARK
from jiuwenswarm.server.runtime.designer.pipeline.image_capacity import image_capacity_for
from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import capacity_for_model


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("三分镜15秒竖屏", MediaIntent(aspect_ratio="9:16", film_duration_sec=15)),
        ("a 5 minute film in 1080p", MediaIntent(resolution="1080p", film_duration_sec=300)),
        ("Shoot it 2.39:1 cinemascope", MediaIntent(aspect_ratio="21:9")),
        ("4:3 retro clip, 4K", MediaIntent(aspect_ratio="4:3", resolution="4k")),
        ("a square video for Instagram", MediaIntent(aspect_ratio="1:1")),
        ("a father and son talk at dinner", MediaIntent()),
    ],
)
def test_regex_reads_explicit_format(prompt: str, expected: MediaIntent) -> None:
    assert regex_media_intent(prompt) == expected


async def test_model_answer_wins_and_patterns_fill_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_call_model_tool(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "text": json.dumps({"aspect_ratio": "9:16", "resolution": None, "film_duration_sec": 90}),
        }

    monkeypatch.setattr(model_tools, "call_model_tool", fake_call_model_tool)
    intent = await extract_media_intent("抖音风格，一分半，720p 横屏")

    assert intent == MediaIntent(aspect_ratio="9:16", resolution="720p", film_duration_sec=90)
    assert json.loads(captured["prompt"]) == {"user_prompt": "抖音风格，一分半，720p 横屏"}


async def test_model_values_outside_the_vocabulary_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call_model_tool(**_kwargs):
        return {
            "ok": True,
            "text": json.dumps({"aspect_ratio": "5:4", "resolution": "8k", "film_duration_sec": True}),
        }

    monkeypatch.setattr(model_tools, "call_model_tool", fake_call_model_tool)
    assert await extract_media_intent("a quiet morning") == MediaIntent()


async def test_model_failure_falls_back_to_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_call_model_tool(**_kwargs):
        raise RuntimeError("billing")

    monkeypatch.setattr(model_tools, "call_model_tool", failing_call_model_tool)
    assert await extract_media_intent("10 second vertical clip") == MediaIntent(
        aspect_ratio="9:16", film_duration_sec=10
    )


def test_settings_from_intent_use_model_values_and_defaults() -> None:
    wan = capacity_for_model("wan3.0-video")
    seedream = image_capacity_for("seedream-5-0-260128", MODELARK)
    video, image = media_settings_from_intent(MediaIntent(), wan, seedream)
    assert video.as_dict() == {"aspect_ratio": "16:9", "resolution": "1080P"}
    assert image.as_dict() == {"aspect_ratio": "16:9", "resolution": "2K"}

    video, image = media_settings_from_intent(
        MediaIntent(aspect_ratio="21:9", resolution="4k", film_duration_sec=120), wan, seedream
    )
    assert video.as_dict() == {"aspect_ratio": "21:9", "resolution": "1080P"}
    assert image.as_dict() == {"aspect_ratio": "16:9", "resolution": "2K"}

    seedance_fast = capacity_for_model("dreamina-seedance-2-0-fast-260128")
    video, image = media_settings_from_intent(MediaIntent(aspect_ratio="9:16", resolution="1080p"), seedance_fast)
    assert video.as_dict() == {"aspect_ratio": "9:16", "resolution": "720P"}
    assert image.aspect_ratio == "9:16"
