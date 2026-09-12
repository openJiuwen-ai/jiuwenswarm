# coding: utf-8
"""Unit tests for real single-harness stage payload builders."""

from openjiuwen.rsi.harness_rsi.single_harness.events_translate import (
    analysis_stage_payload,
    case_stage_payload,
    generate_stage_payload,
)


def test_case_stage_payload_keeps_structured_progress() -> None:
    payload = case_stage_payload(2, 5, "passed", case_id="case-a", score=0.8125)

    assert payload["id"] == "evaluate.case.2"
    assert payload["status"] == "passed"
    assert payload["case_index"] == 2
    assert payload["total_cases"] == 5
    assert payload["case_id"] == "case-a"
    assert payload["score"] == 0.8125
    assert payload["name"].startswith("Case 2/5")


def test_case_stage_payload_defaults_to_running() -> None:
    payload = case_stage_payload(1, 3, "running")

    assert payload["status"] == "running"
    assert payload["name"] == "Case 1/3 evaluating"
    assert "score" not in payload


def test_generate_stage_payload_reports_done() -> None:
    payload = generate_stage_payload(2, 4, "done")

    assert payload["id"] == "generate.candidate"
    assert payload["status"] == "done"
    assert payload["candidate_index"] == 2
    assert payload["total_candidates"] == 4
    assert payload["name"] == "Candidate 2/4 generated"


def test_analysis_stage_payload_reports_running() -> None:
    payload = analysis_stage_payload("running", failed_case_count=2)

    assert payload["id"] == "analyze.failures"
    assert payload["status"] == "running"
    assert payload["failed_case_count"] == 2
    assert payload["name"] == "Analyzing failed cases"
