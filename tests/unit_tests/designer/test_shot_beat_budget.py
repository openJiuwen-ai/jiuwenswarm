# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
    count_narrative_beats,
    infer_shot_budget,
)
from jiuwenswarm.server.runtime.designer.script_analysis import heuristic_analysis

CN_MULTI_BEAT = (
    "漆黑冰冷的北冥深海掀起万里海啸，乌云在天空中翻滚。"
    "一头巨大如黑色大陆的远古巨兽鲲从深海中缓缓浮现，激起滔天巨浪。"
    "紧接着，巨兽的脊背在电闪雷鸣中裂开，暗金羽毛疯狂生长。"
    "它展开遮天蔽日的双翼，化为远古大鹏鸟，扶摇直上九万里。"
    "镜头由远景快速拉近，特写大鹏鸟金色的瞳孔与巨型羽翼。"
)


def test_chinese_prose_beats_are_counted():
    """CJK has no word boundaries, so \\b cues never fired and every prompt read as 1 beat."""
    assert count_narrative_beats(CN_MULTI_BEAT) >= 4


def test_keyframe_count_is_an_explicit_contract():
    """One shot == one keyframe + one clip, so "N 个关键帧" must be honored as N."""
    from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
        _explicit_shot_count_from_prompt,
    )

    assert _explicit_shot_count_from_prompt("请用5个关键帧") == 5
    assert _explicit_shot_count_from_prompt("用三个关键帧收束") == 3
    assert _explicit_shot_count_from_prompt("use 6 keyframes") == 6
    assert _explicit_shot_count_from_prompt(CN_MULTI_BEAT + " 请用5个关键帧。") == 5
    assert _explicit_shot_count_from_prompt(CN_MULTI_BEAT + " 请拆成3个分镜。") == 3


def test_short_prompt_stays_lean():
    analysis = heuristic_analysis("A quiet portrait of a woman in an office.")
    assert len(analysis["shots"]) == 1


def test_placeholder_shot_count_is_a_floor_not_a_ceiling():
    """A failed opening LLM call must not pin the film to its 1-shot skeleton."""
    placeholder = {
        "source": "heuristic_pending_llm",
        "llm_pending": True,
        "target_shot_count": 1,
        "shots": [{"shot_index": 1, "action": CN_MULTI_BEAT}],
    }
    assert infer_shot_budget(CN_MULTI_BEAT, placeholder) >= 4


def test_director_authored_shot_count_is_respected():
    authored = {
        "source": "llm",
        "target_shot_count": 2,
        "shots": [{"shot_index": 1}, {"shot_index": 2}],
    }
    assert infer_shot_budget(CN_MULTI_BEAT, authored) == 2
