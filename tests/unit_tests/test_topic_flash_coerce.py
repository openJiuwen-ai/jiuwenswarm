# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash-friendly coerce for topic memory curation and RID decisions."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    validate_memory_incremental,
    validate_memory_init,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    RID_API_PROMPT_VERSION,
    RID_COMPONENT_ID,
    LearnedComponentBinding,
    LongTermProfileInterestInput,
    distill_trigger_interests,
)


def _unit(
    unit_id: str,
    *,
    pattern: str = "tech-ai-ml",
    indices: list[int] | None = None,
) -> dict:
    return {
        "core": {
            "behavior_pattern": pattern,
            "preference_summary": "关注 AI 测试能力",
            "representative_behavior_indices": indices or [1],
            "preferred_entities": ["AI测试"],
            "temporal_activity": "ongoing",
            "timestamp": 1.0,
        },
        "companion": {"unit_id": unit_id},
    }


def test_memory_init_rewrites_chinese_pattern_to_shared_tag() -> None:
    cores, reason = validate_memory_init(
        {
            "units": [
                {
                    "behavior_pattern": "人工智能",
                    "preference_summary": "想掌握 AI 测试",
                    "representative_behavior_indices": [1, "2"],
                    "preferred_entities": ["AI测试", 12],
                    "temporal_activity": None,
                    "note": "drop me",
                }
            ],
            "commentary": "ignore",
        },
        {1, 2},
        mapping_by_index={
            1: {"tag": "tech-ai-ml", "action_type": "natural_question"},
            2: {"tag": "tech-ai-ml", "action_type": "follow_up"},
        },
    )
    assert reason == ""
    assert cores is not None
    assert cores[0]["behavior_pattern"] == "tech-ai-ml"
    assert cores[0]["representative_behavior_indices"] == [1, 2]
    assert cores[0]["preferred_entities"] == ["AI测试"]
    assert cores[0]["temporal_activity"] == ""


def test_memory_init_drops_bad_indices_not_whole_unit() -> None:
    cores, reason = validate_memory_init(
        {
            "units": [
                {
                    "behavior_pattern": "人工智能",
                    "preference_summary": "保留可解析 index",
                    "representative_behavior_indices": [1, None, "b3", 0, "2"],
                    "preferred_entities": [],
                    "temporal_activity": "",
                }
            ],
        },
        {1, 2},
        mapping_by_index={
            1: {"tag": "tech-ai-ml", "action_type": "natural_question"},
            2: {"tag": "tech-ai-ml", "action_type": "follow_up"},
        },
    )
    assert reason == ""
    assert cores is not None
    assert cores[0]["representative_behavior_indices"] == [1, 2]


def test_memory_init_empty_indices_after_coerce_fail_closed() -> None:
    cores, reason = validate_memory_init(
        {
            "units": [
                {
                    "behavior_pattern": "tech-ai-ml",
                    "preference_summary": "坏 index 剔空后应失败",
                    "representative_behavior_indices": [None, "x", 0],
                    "preferred_entities": [],
                    "temporal_activity": "",
                }
            ],
        },
        {1},
        mapping_by_index={1: {"tag": "tech-ai-ml", "action_type": "natural_question"}},
    )
    assert cores is None
    assert reason == "memory_indices_invalid"


def test_memory_incremental_coerces_empty_update_and_wrong_new_pattern() -> None:
    previous = [_unit("u1", indices=[1])]
    mapping = {
        1: {"tag": "tech-ai-ml", "action_type": "natural_question"},
        2: {
            "tag": "tech-ai-ml",
            "action_type": "explicit_goal_preference_task",
        },
        3: {
            "tag": "tech-software-dev",
            "action_type": "explicit_goal_preference_task",
        },
    }
    result, reason = validate_memory_incremental(
        {
            "updated_units": [
                {
                    "unit_id": "u1",
                    "operation": "update",
                    "core": {
                        "behavior_pattern": "人工智能",
                        "preference_summary": "仍关注 AI",
                        "representative_behavior_indices": [1],
                        "preferred_entities": [],
                        "temporal_activity": "ongoing",
                    },
                }
            ],
            "new_units": [
                {
                    "behavior_pattern": "软件工程",
                    "preference_summary": "想学测试框架",
                    "representative_behavior_indices": [3],
                    "preferred_entities": ["pytest"],
                    "temporal_activity": "new",
                }
            ],
            "unmatched_behavior_indices": [],
            "extra": "noise",
        },
        previous,
        {2, 3},
        {1, 2, 3},
        mapping,
    )
    assert reason == ""
    assert result is not None
    assert result["operations"]["u1"]["operation"] == "retain"
    assert len(result["new_cores"]) == 1
    assert result["new_cores"][0]["behavior_pattern"] == "tech-software-dev"
    assert result["consumed"] == {3}
    assert result["unmatched"] == {2}


@pytest.mark.asyncio
async def test_rid_coerces_flash_sloppy_decisions() -> None:
    interests = (
        LongTermProfileInterestInput(
            interest_summary_id="pi-1",
            context_scope="global_new_chat",
            profile_semantic_version=1,
            interest_cluster="tech-ai-ml",
            summary_text="AI 测试",
            source_profile_unit_ids=("unit-1",),
        ),
        LongTermProfileInterestInput(
            interest_summary_id="pi-2",
            context_scope="global_new_chat",
            profile_semantic_version=1,
            interest_cluster="tech-software-dev",
            summary_text="测试框架",
            source_profile_unit_ids=("unit-2",),
        ),
    )
    binding = LearnedComponentBinding(
        component_id=RID_COMPONENT_ID,
        provider_id="ark",
        provider_version="v1",
        config_version=RID_API_PROMPT_VERSION,
    )

    async def _distiller(_payload: dict) -> dict:
        return {
            "reasoning": "flash chatter",
            "decisions": [
                {
                    "interest_summary_id": "pi-2",
                    "selected": "true",
                    "rank": "2",
                    "resonance_score_nullable": 0.88,
                    "why": "stronger",
                },
                {
                    "interest_summary_id": "pi-1",
                    "selected": 1,
                    "rank": 5,
                    "resonance_score_nullable": None,
                },
            ],
        }

    result, reason = await distill_trigger_interests(
        user_profile={},
        long_term_profile_interests=interests,
        session_summaries=(),
        input_snapshot_id="snap-1",
        request_id="req-1",
        activity_group="steady",
        threshold_version="icebreaker-api-proxy-threshold-v1",
        delta=[],
        feedback_context={},
        binding=binding,
        invoke_distiller=_distiller,
    )
    assert reason == ""
    assert result is not None
    selected = [(item.interest_summary_id, item.rank) for item in result.trigger_interests]
    assert selected == [("pi-2", 1), ("pi-1", 2)]
    assert {item.interest_summary_id for item in result.rejected_interests} == set()


@pytest.mark.asyncio
async def test_rid_all_dropped_decisions_contract_fail_not_empty_success() -> None:
    interests = (
        LongTermProfileInterestInput(
            interest_summary_id="pi-1",
            context_scope="global_new_chat",
            profile_semantic_version=1,
            interest_cluster="tech-ai-ml",
            summary_text="AI 测试",
            source_profile_unit_ids=("unit-1",),
        ),
    )
    binding = LearnedComponentBinding(
        component_id=RID_COMPONENT_ID,
        provider_id="ark",
        provider_version="v1",
        config_version=RID_API_PROMPT_VERSION,
    )

    async def _distiller(_payload: dict) -> dict:
        return {
            "decisions": [
                {
                    "interest_summary_id": "pi-guitar",
                    "selected": True,
                    "rank": 1,
                    "resonance_score_nullable": None,
                },
                {
                    "interest_summary_id": "pi-1",
                    "selected": "maybe",
                    "rank": 2,
                    "resonance_score_nullable": None,
                },
            ],
        }

    result, reason = await distill_trigger_interests(
        user_profile={},
        long_term_profile_interests=interests,
        session_summaries=(),
        input_snapshot_id="snap-1",
        request_id="req-1",
        activity_group="steady",
        threshold_version="icebreaker-api-proxy-threshold-v1",
        delta=[],
        feedback_context={},
        binding=binding,
        invoke_distiller=_distiller,
    )
    assert result is None
    assert reason == "rid_output_contract_failed"


@pytest.mark.asyncio
async def test_rid_legitimate_empty_decisions_still_succeeds() -> None:
    interests = (
        LongTermProfileInterestInput(
            interest_summary_id="pi-1",
            context_scope="global_new_chat",
            profile_semantic_version=1,
            interest_cluster="tech-ai-ml",
            summary_text="AI 测试",
            source_profile_unit_ids=("unit-1",),
        ),
    )
    binding = LearnedComponentBinding(
        component_id=RID_COMPONENT_ID,
        provider_id="ark",
        provider_version="v1",
        config_version=RID_API_PROMPT_VERSION,
    )

    async def _distiller(_payload: dict) -> dict:
        return {"decisions": []}

    result, reason = await distill_trigger_interests(
        user_profile={},
        long_term_profile_interests=interests,
        session_summaries=(),
        input_snapshot_id="snap-1",
        request_id="req-1",
        activity_group="steady",
        threshold_version="icebreaker-api-proxy-threshold-v1",
        delta=[],
        feedback_context={},
        binding=binding,
        invoke_distiller=_distiller,
    )
    assert reason == ""
    assert result is not None
    assert result.trigger_interests == ()
    assert {item.interest_summary_id for item in result.rejected_interests} == {"pi-1"}
