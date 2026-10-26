# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the canonical AskUser response contract."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.ask_user import (
    AskUserResponseError,
    normalize_ask_user_response,
    parse_ask_user_response,
)


def test_normalize_answered_response_preserves_one_structured_representation():
    response = normalize_ask_user_response(
        status="answered",
        answers=[
            {
                "question": " Enable which modules? ",
                "selected_options": ["auth", "Other"],
                "custom_input": " metrics ",
            }
        ],
        original_request=" Build the service ",
    )

    assert response.to_dict() == {
        "status": "answered",
        "answers": [
            {
                "question": "Enable which modules?",
                "selected_options": ["auth"],
                "custom_input": "metrics",
            }
        ],
        "original_request": "Build the service",
    }
    assert response.to_readable_text() == "Enable which modules?: auth, metrics"


@pytest.mark.parametrize("label", ["Other", "其他"])
def test_explicit_answered_bare_other_preserves_empty_answered_contract(label: str):
    response = normalize_ask_user_response(
        status="answered",
        answers=[
            {
                "question": "Which?",
                "selected_options": [label],
                "custom_input": "  ",
            }
        ],
    )

    assert response.to_dict() == {"status": "answered", "answers": []}


def test_normalize_missing_status_derives_answered_from_current_array_protocol():
    response = normalize_ask_user_response(
        status="",
        answers=[
            {
                "question": "",
                "selected_options": [],
                "custom_input": "plain answer",
            }
        ],
    )

    assert response.status == "answered"
    assert response.to_readable_text() == "plain answer"


def test_normalize_missing_status_defaults_to_answered_for_current_native_clients():
    response = normalize_ask_user_response(
        status="",
        answers=[
            {
                "question": "Which?",
                "selected_options": ["Other"],
                "custom_input": "  ",
            }
        ],
    )

    assert response.to_dict() == {"status": "answered", "answers": []}


def test_explicit_answered_without_user_content_is_preserved():
    response = normalize_ask_user_response(status="answered", answers=[])

    assert response.to_dict() == {"status": "answered", "answers": []}


def test_skipped_with_user_content_is_rejected():
    with pytest.raises(AskUserResponseError, match="skipped response"):
        normalize_ask_user_response(
            status="skipped",
            answers=[
                {
                    "question": "Which?",
                    "selected_options": ["A"],
                    "custom_input": None,
                }
            ],
        )


@pytest.mark.parametrize(
    "answers",
    [
        {"Which?": "A"},
        ["A"],
        [{"question": "Which?", "selected_options": "A"}],
        [{"question": "Which?", "selected_options": [1]}],
    ],
)
def test_malformed_or_legacy_answers_are_rejected(answers):
    with pytest.raises(AskUserResponseError):
        normalize_ask_user_response(status="answered", answers=answers)


def test_parse_rejects_legacy_sidecar_and_answer_map():
    with pytest.raises(AskUserResponseError):
        parse_ask_user_response(
            {
                "answers": {"Which?": "A"},
                "_structured_response": {
                    "status": "answered",
                    "answers": [],
                },
            }
        )


def test_parse_accepts_and_drops_unknown_extension_fields():
    response = parse_ask_user_response(
        {
            "status": "answered",
            "answers": [
                {
                    "question": "Which?",
                    "selected_options": ["A"],
                    "custom_input": None,
                    "answer_metadata": {"display_order": 1},
                }
            ],
            "client_version": "1.2",
        }
    )

    assert response.to_dict() == {
        "status": "answered",
        "answers": [
            {
                "question": "Which?",
                "selected_options": ["A"],
                "custom_input": None,
            }
        ],
    }
