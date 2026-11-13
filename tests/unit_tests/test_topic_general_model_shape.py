# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""General-model shape coerce / repair helpers for topic recommendation."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    CanonicalBehavior,
    coerce_mapping_batch_payload,
    validate_mapping_batch,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    invoke_stage_with_shape_repair,
    model_ids_compatible,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_selection import (
    coerce_diversity_scorer_payload,
    coerce_utility_scorer_payload,
    utility_scores_by_id,
)


def test_model_ids_compatible_accepts_route_prefix() -> None:
    assert model_ids_compatible("models/Qwen3.8-Flash", "Qwen3.8-Flash")
    assert model_ids_compatible("Qwen3.8-Flash", "Qwen3.8-Flash")
    assert not model_ids_compatible(None, "Qwen3.8-Flash")
    assert not model_ids_compatible("other-model", "Qwen3.8-Flash")


def test_utility_coerce_drops_extras_and_loosens_types() -> None:
    raw = coerce_utility_scorer_payload(
        {
            "scores": [
                {
                    "candidate_id": "c1",
                    "question_focus": "唐朝皇帝数量口径",
                    "assertion_spans": [],
                    "given_conditions": [],
                    "click_score_nullable": "0.5",
                    "depth_score_nullable": "null",
                    "combined_utility_nullable": 0.4,
                    "admitted": "true",
                    "reason": "none; new historical fact question",
                    "commentary": "drop me",
                }
            ],
            "note": "drop",
        }
    )
    by_id = utility_scores_by_id(raw, ["c1"])
    assert by_id["c1"].admitted is True
    assert by_id["c1"].click_score_nullable == 0.5
    assert by_id["c1"].depth_score_nullable is None


def test_utility_coerce_missing_arrays_default_empty() -> None:
    raw = coerce_utility_scorer_payload(
        {
            "scores": [
                {
                    "candidate_id": "c1",
                    "question_focus": "开放问题",
                    "click_score_nullable": 0.2,
                    "depth_score_nullable": 0.2,
                    "combined_utility_nullable": 0.2,
                    "admitted": True,
                    "reason": "none; open comparison",
                }
            ]
        }
    )
    by_id = utility_scores_by_id(raw, ["c1"])
    assert by_id["c1"].given_conditions == ()


def test_utility_coerce_bare_list_and_field_aliases() -> None:
    raw = coerce_utility_scorer_payload(
        [
            {
                "id": "c1",
                "question_focus": "开放比较",
                "assertion_spans": "题面断言",
                "click_score": "0.3",
                "depth_score": None,
                "combined_utility": "0.25",
                "admitted": "yes",
                "reason": "none; open comparison",
            }
        ]
    )
    assert raw["scores"][0]["assertion_spans"] == ["题面断言"]
    by_id = utility_scores_by_id(raw, ["c1"])
    assert by_id["c1"].candidate_id == "c1"
    assert by_id["c1"].click_score_nullable == 0.3
    assert by_id["c1"].combined_utility_nullable == 0.25
    assert by_id["c1"].admitted is True


def test_utility_coerce_items_wrapper_defaults_nullable_scores() -> None:
    raw = coerce_utility_scorer_payload(
        {
            "items": [
                {
                    "candidate_id": "c1",
                    "question_focus": "缺分数字段",
                    "admitted": True,
                    "reason": "none; still scorable",
                }
            ]
        }
    )
    by_id = utility_scores_by_id(raw, ["c1"])
    assert by_id["c1"].click_score_nullable is None
    assert by_id["c1"].depth_score_nullable is None
    assert by_id["c1"].combined_utility_nullable is None


def test_diversity_coerce_bool_and_scores() -> None:
    raw = coerce_diversity_scorer_payload(
        {
            "pairwise_scores": [
                {
                    "left_candidate_id": "a",
                    "right_candidate_id": "b",
                    "compatible": "false",
                    "diversity_score_nullable": "0.1",
                    "reason": "same explanation",
                    "extra": 1,
                }
            ],
            "marginal_scores": [
                {
                    "candidate_id": "a",
                    "marginal_diversity_nullable": "0.3",
                },
                {
                    "candidate_id": "b",
                    "marginal_diversity_nullable": None,
                },
            ],
            "noise": True,
        }
    )
    assert set(raw) == {"pairwise_scores", "marginal_scores"}
    assert raw["pairwise_scores"][0]["compatible"] is False
    assert raw["pairwise_scores"][0]["diversity_score_nullable"] == 0.1
    assert "extra" not in raw["pairwise_scores"][0]


def test_mapping_coerce_index_and_empty_span() -> None:
    batch = [
        CanonicalBehavior(
            behavior_index=1,
            session_id="s",
            ordinal=0,
            content="唐朝有多少个皇帝",
            timestamp_or_0=1.0,
        )
    ]
    payload = coerce_mapping_batch_payload(
        {
            "mappings": [
                {
                    "behavior_index": "1",
                    "tag": "culture-history-humanities",
                    "action_type": "natural_question",
                    "literal_focus_span": "",
                    "extra": "x",
                }
            ],
            "meta": 1,
        }
    )
    entries, reason = validate_mapping_batch(payload, batch)
    assert reason == ""
    assert entries is not None
    assert entries[1]["literal_focus_span"] is None


@pytest.mark.asyncio
async def test_shape_repair_retries_once_then_succeeds() -> None:
    calls: list[str] = []

    async def invoke(stage: str, system: str, payload: str) -> dict:
        calls.append(payload)
        if len(calls) == 1:
            return {"scores": [{"candidate_id": "c1", "admitted": "true"}]}
        return {
            "scores": [
                {
                    "candidate_id": "c1",
                    "question_focus": "修好了",
                    "assertion_spans": [],
                    "given_conditions": [],
                    "click_score_nullable": 0.1,
                    "depth_score_nullable": 0.1,
                    "combined_utility_nullable": 0.1,
                    "admitted": True,
                    "reason": "none; repaired",
                }
            ]
        }

    def validate(raw: object) -> None:
        utility_scores_by_id(raw, ["c1"])

    result = await invoke_stage_with_shape_repair(
        invoke,
        "v6_utility_scorer",
        "system",
        '{"request_id":"repair-req-1"}',
        coerce=coerce_utility_scorer_payload,
        validate=validate,
    )
    assert len(calls) == 2
    assert "validation_error" in calls[1]
    assert '"request_id": "repair-req-1"' in calls[1] or '"request_id":"repair-req-1"' in calls[1]
    assert result["scores"][0]["candidate_id"] == "c1"


@pytest.mark.asyncio
async def test_shape_repair_retries_on_json_decode_error() -> None:
    calls: list[str] = []

    async def invoke(stage: str, system: str, payload: str) -> dict:
        calls.append(payload)
        if len(calls) == 1:
            raise json.JSONDecodeError("Expecting value", "{garbage", 1)
        return {
            "scores": [
                {
                    "candidate_id": "c1",
                    "question_focus": "修好了",
                    "assertion_spans": [],
                    "given_conditions": [],
                    "click_score_nullable": 0.1,
                    "depth_score_nullable": 0.1,
                    "combined_utility_nullable": 0.1,
                    "admitted": True,
                    "reason": "none; repaired after parse failure",
                }
            ]
        }

    def validate(raw: object) -> None:
        utility_scores_by_id(raw, ["c1"])

    result = await invoke_stage_with_shape_repair(
        invoke,
        "v6_utility_scorer",
        "system",
        '{"request_id":"repair-json-1"}',
        coerce=coerce_utility_scorer_payload,
        validate=validate,
    )
    assert len(calls) == 2
    assert "JSON parse failed" in calls[1]
    assert '"request_id": "repair-json-1"' in calls[1] or '"request_id":"repair-json-1"' in calls[1]
    assert result["scores"][0]["candidate_id"] == "c1"
