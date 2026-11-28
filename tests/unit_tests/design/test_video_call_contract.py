# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Video call preparation preserves authored prose and exact dialogue."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks


@pytest.mark.parametrize(
    "prompt",
    [
        "镜头缓慢推近，咖啡师穿白衬衫和棕色围裙，站在吧台左侧擦拭杯子。",
        "Sam stays by the window in his grey coat. The camera holds still.",
        "PREVIOUS CLIP HAD: Sam left. YOUR ASSIGNMENT: hold on the empty doorway.",
        "STYLE LOCK: watercolor. Do not add extras. Sam reaches for the blue flask.",
        "阿岚回头。",
        "镜头缓慢推近，人物微笑。" * 300,
    ],
)
def test_authored_video_prompt_is_not_semantically_rewritten(prompt: str) -> None:
    cfg = {
        "clip_action": "A different storyboard action",
        "previous_clip_action": prompt,
        "cast_names": ["A character omitted from this framing"],
        "costume_lock": "navy sweater",
        "language_lock": "zh",
        "time_of_day_lock": {"time_of_day": "morning"},
    }
    assert apply_wan_call_locks(prompt, cfg=cfg, graph={}) == prompt
    assert cfg["last_wan_prompt"] == prompt


@pytest.mark.parametrize("mode", ["i2v", "r2v"])
def test_reference_led_video_prompt_is_also_preserved(mode: str) -> None:
    prompt = "承接上一镜，女孩走出花园，镜头停在门边的风铃上。"
    cfg = {"reference_call_mode": mode, "clip_action": "different wording", "lighting": "warm"}
    assert apply_wan_call_locks(prompt, cfg=cfg) == prompt


@pytest.mark.parametrize("prompt", [None, "", "  ", [], {"action": "wave"}])
def test_invalid_video_prompt_fails_before_stamping(prompt: object) -> None:
    cfg = {}
    with pytest.raises(ValueError, match="non-empty string"):
        apply_wan_call_locks(prompt, cfg=cfg)
    assert "last_wan_prompt" not in cfg


@pytest.mark.parametrize("speech", [{"char_1": ["hello"]}, ["hello"]])
def test_invalid_dialogue_structure_is_rejected(speech: object) -> None:
    with pytest.raises(ValueError, match="must be"):
        apply_wan_call_locks("Sam waves.", cfg={"speech_by_character": speech})


def test_missing_dialogue_is_appended_once_without_changing_the_original_prompt() -> None:
    prompt = '近景，阿岚微笑着说：“早上好。”镜头保持稳定。'
    graph = {"metadata": {"script_analysis": {"characters": [
        {"id": "char_1", "name": "阿岚"},
        {"id": "char_2", "name": "小林"},
    ]}}}
    cfg = {"speech_by_character": {"char_1": "早上好。", "char_2": "来一杯咖啡，谢谢。"}}
    sent = apply_wan_call_locks(prompt, cfg=cfg, graph=graph)
    assert sent == prompt + '\n\nDialogue: 小林: "来一杯咖啡，谢谢。"'
    assert apply_wan_call_locks(sent, cfg=cfg, graph=graph) == sent


def test_serialized_speech_line_checks_words_without_the_speaker_id_prefix() -> None:
    graph = {"metadata": {"script_analysis": {"characters": [{"id": "char_1", "name": "Sam"}]}}}
    cfg = {"speech_line": "char_1: Let's go!"}
    prompt = 'Sam turns toward the door and says, "Let\'s go!"'
    assert apply_wan_call_locks(prompt, cfg=cfg, graph=graph) == prompt


def test_long_explicit_dialogue_is_not_truncated() -> None:
    line = "这是明确要求保留的完整台词。" * 30
    cfg = {"speech_by_character": {"narrator": line}}
    sent = apply_wan_call_locks("镜头保持稳定。", cfg=cfg)
    assert line in sent
