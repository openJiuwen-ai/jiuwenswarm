"""Evaluation fixtures stay data. They are not model input and not accuracy tests."""

import json
from pathlib import Path

CASES_PATH = Path(__file__).parents[1] / "examples" / "labeled_cases.json"

REQUIRED_IDS = {
    "correct_current_input",
    "compatible_future_export",
    "affected_recipient",
    "unaffected_recipient",
    "correction_already_applied",
    "joint_correction_and_addition",
    "explicitly_idle",
    "mode_claim_without_valid_change",
}


def test_labeled_cases_are_well_formed_evaluation_data():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert REQUIRED_IDS <= set(ids)
    by_id = {}
    for case in cases:
        assert set(case) == {"id", "context", "messages", "expected", "rationale"}
        assert case["expected"] in ("INTERRUPT", "APPEND")
        assert isinstance(case["context"], str) and case["context"].strip()
        assert isinstance(case["messages"], list) and case["messages"]
        assert all(isinstance(message, str) and message.strip() for message in case["messages"])
        assert isinstance(case["rationale"], str) and case["rationale"].strip()
        by_id[case["id"]] = case

    shared = by_id["affected_recipient"]["messages"]
    assert by_id["unaffected_recipient"]["messages"] == shared
    assert by_id["affected_recipient"]["expected"] == "INTERRUPT"
    assert by_id["unaffected_recipient"]["expected"] == "APPEND"
    assert by_id["affected_recipient"]["context"] != by_id["unaffected_recipient"]["context"]
