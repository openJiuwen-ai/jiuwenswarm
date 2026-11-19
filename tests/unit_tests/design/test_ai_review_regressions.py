# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.handlers.text_nodes import _audio_plan_instruction
from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import infer_crowd_state
from jiuwenswarm.server.runtime.designer.script_analysis import detect_cast_form


def test_chinese_count_only_counts_people() -> None:
    # CR-001: a number plus a classifier is not a headcount unless people are counted.
    assert detect_cast_form("Harbour clerk", "推着三个货架搬货", "")[0] == "single"
    assert detect_cast_form("Data steward", "整理三组数据", "")[0] == "single"
    # People after the classifier still make a group, with or without a collective noun.
    assert detect_cast_form("Volunteers", "一群孩子", "")[0] == "group"
    assert detect_cast_form("Elders", "三位老人坐在树下", "") == ("group", "三位")
    assert detect_cast_form("Shoppers", "三组顾客在挑衣服", "")[0] == "group"


def test_silent_audio_plan_promises_no_speech_or_room_tone() -> None:
    # CR-002: the brief instruction follows the plan instead of assuming sound.
    plan = {
        "speech": {"include": False, "kind": "none"},
        "sfx": {"include": False},
        "music": {"requested": False, "supported": False},
    }
    lines = _audio_plan_instruction(plan)
    assert "Speech: none" in lines
    assert "narration / dialogue in the locked language" not in lines
    assert "Diegetic sound: none" in lines
    assert "room tone and action sound only" not in lines
    assert "Music: none." in lines


def test_speech_and_sound_plan_is_unchanged() -> None:
    plan = {
        "speech": {"include": True, "kind": "dialogue"},
        "sfx": {"include": True},
        "music": {"requested": True, "supported": True},
    }
    lines = _audio_plan_instruction(plan)
    assert "Speech: narration / dialogue in the locked language." in lines
    assert "Diegetic sound: room tone and action sound only." in lines
    assert "one film-level score" in lines


def test_returned_group_is_present_and_keeps_its_sentence() -> None:
    # CR-004/CR-005: "returned" means the crowd is back, in every consumer.
    state = infer_crowd_state({"crowd_lock": {"disposition": "returned"}})
    assert state["present"] is True
    assert state["disposition"] == "returned"
    assert state["hold"]
