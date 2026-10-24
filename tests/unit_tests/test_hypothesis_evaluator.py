# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the multi-dimensional hypothesis evaluator.

Exercises the pure-function entry point and the stateful evaluator: keyword
scoring, threshold boundaries, LLM fusion, batch ranking and history.
"""

from __future__ import annotations

from jiuwenswarm.symphony.evolution import (
    HypothesisEvaluator,
    HypothesisScore,
    evaluate_hypothesis,
)
from jiuwenswarm.symphony.evolution.hypothesis_evaluator import (
    MIN_THRESHOLD,
    RECOMMEND_THRESHOLD,
)

# Every keyword bucket is hit so each axis clamps to 10.
_MAXED_HYPOTHESIS = {
    "title": "novel experiment",
    "problem": "validate important",
    "approach": "implement benchmark deployment",
    "novelty": "new first unique unprecedented",
    "feasibility": "evaluate",
    "impact": "significant practical real-world",
}


def test_rule_based_keywords_clamp_each_axis_to_ten():
    score = evaluate_hypothesis(_MAXED_HYPOTHESIS)
    assert (score.feasibility, score.novelty, score.impact) == (10, 10, 10)
    assert score.total == 30
    assert score.recommendation == "strong_recommend"


def test_empty_hypothesis_uses_baseline_and_is_conditional():
    score = evaluate_hypothesis({})
    assert (score.feasibility, score.novelty, score.impact) == (5, 5, 5)
    assert score.total == 15
    assert score.recommendation == "conditional"


def test_recommendation_threshold_boundaries():
    # 21 => strong, 20/15 => conditional, 14 => reject
    assert HypothesisScore(7, 7, 7).recommendation == "strong_recommend"
    assert HypothesisScore(7, 7, 6).recommendation == "conditional"
    assert HypothesisScore(5, 5, 5).recommendation == "conditional"
    assert HypothesisScore(5, 5, 4).recommendation == "reject"
    assert RECOMMEND_THRESHOLD == 21
    assert MIN_THRESHOLD == 15


def test_to_dict_exposes_total_and_recommendation():
    payload = HypothesisScore(8, 6, 7, reasoning="because").to_dict()
    assert payload["total"] == 21
    assert payload["recommendation"] == "strong_recommend"
    assert payload["reasoning"] == "because"
    assert set(payload) == {
        "feasibility",
        "novelty",
        "impact",
        "total",
        "recommendation",
        "reasoning",
    }


def test_llm_callback_is_averaged_with_rule_score():
    def llm_callback(_hypothesis):
        return {"feasibility": 10, "novelty": 10, "impact": 10, "reasoning": "deep"}

    score = evaluate_hypothesis(
        {"title": "novel experiment"}, llm_callback=llm_callback
    )
    # rule scores for this input are feasibility=6, novelty=6, impact=5
    assert (score.feasibility, score.novelty, score.impact) == (8, 8, 7)
    assert "LLM" in score.reasoning


def test_llm_callback_failure_falls_back_to_rule_score():
    def boom(_hypothesis):
        raise RuntimeError("model unavailable")

    score = evaluate_hypothesis({"title": "novel experiment"}, llm_callback=boom)
    assert score.total == 17  # 6 + 6 + 5, untouched


def test_batch_ranking_and_select_best():
    evaluator = HypothesisEvaluator()
    strong = dict(_MAXED_HYPOTHESIS)
    weak = {"title": "meh"}

    ranked = evaluator.evaluate_batch([weak, strong])
    assert ranked[0][0] is strong
    assert ranked[0][1].total > ranked[1][1].total

    # nothing clears an unreachable bar
    assert evaluator.select_best([weak], min_score=100) is None
    # the strong hypothesis clears the default bar and is annotated
    chosen = evaluator.select_best([weak, strong])
    assert chosen is not None
    assert chosen["_score"]["recommendation"] == "strong_recommend"


def test_select_best_does_not_mutate_the_input_hypothesis():
    evaluator = HypothesisEvaluator()
    strong = dict(_MAXED_HYPOTHESIS)

    chosen = evaluator.select_best([strong])

    assert chosen is not None
    assert chosen["_score"]["recommendation"] == "strong_recommend"
    # copy-on-score: the caller's dict must be left untouched
    assert chosen is not strong
    assert "_score" not in strong


def test_history_records_each_evaluation():
    evaluator = HypothesisEvaluator()
    evaluator.evaluate({"title": "alpha"})
    evaluator.evaluate({"title": "beta"})
    assert [entry["hypothesis"] for entry in evaluator.history] == ["alpha", "beta"]
