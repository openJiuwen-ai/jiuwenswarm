"""Comparison integrity checks, independent of optional Docker/model dependencies."""
import copy

import pytest

from jiuwenswarm.benchmarks.duplex_workloads import schedule, summarize_trials


def trial(policy, score, seconds=10):
    return {"suite": "office", "task_id": "task", "repeat": 0, "policy": policy,
            "quality": {"score": score, "success": None if score is None else score == 1},
            "execution": {"agent_seconds": seconds}, "comparison_key": "fixed",
            "images": {"task": "digest"}}


def test_missing_official_grade_is_not_a_loss_or_a_comparable_pair():
    result = summarize_trials([trial("steer", None), trial("model", 1)])['office']
    assert result["groups"]["steer"]["mean_score"] is None
    assert result["groups"]["steer"]["unscored"] == 1
    assert result["paired_scored"] == 0
    assert result["mean_paired_score_delta"] is None


def test_only_identical_settings_and_images_form_a_pair():
    a, b = trial("steer", 0.5, 20), trial("model", 1, 10)
    result = summarize_trials([a, b])["office"]
    assert result["mean_paired_score_delta"] == 0.5
    assert result["median_time_ratio_model_over_steer"] == 0.5
    assert result["median_time_ratio_both_success"] is None
    for field in ("comparison_key", "images"):
        changed = copy.deepcopy(b)
        changed[field] = "different"
        result = summarize_trials([a, changed])["office"]
        assert result["paired_scored"] == 0
        assert result["incomparable_pairs"] == 1


def test_duplicate_trials_are_rejected_and_order_is_balanced():
    with pytest.raises(ValueError, match="Duplicate"):
        summarize_trials([trial("steer", 1), trial("steer", 1)])
    plan = schedule({"office": [{"task_id": "a"}, {"task_id": "b"}]}, repeats=2)
    assert len(plan) == 8
    assert [r["policy"] for r in plan] == ["steer", "model", "model", "steer", "model", "steer", "steer", "model"]
