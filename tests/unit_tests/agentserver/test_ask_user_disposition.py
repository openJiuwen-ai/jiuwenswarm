# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask-user Skip/Cancel disposition sentinels and contract normalization."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.ask_user_contract import (
    build_ask_user_answers_dict,
)
from jiuwenswarm.agents.harness.common.rails.ask_user_disposition import (
    CANCELLED,
    SKIPPED,
    is_skipped_or_cancelled,
    normalize_disposition_text,
)


@pytest.mark.parametrize(
    ("disposition", "custom_input", "expected"),
    [
        ("skipped", "", SKIPPED),
        ("cancelled", "", CANCELLED),
        ("skipped", "ignored", SKIPPED),
        (None, SKIPPED, SKIPPED),
        (None, CANCELLED, CANCELLED),
        (None, "用户已跳过此题，未选择任何选项。", SKIPPED),
        (None, "Skipped. No option selected.", SKIPPED),
        (None, "用户已取消本次问答，未作答。", CANCELLED),
        (None, "Cancelled. No answer provided.", CANCELLED),
        (None, "ordinary answer", None),
        (None, "", None),
    ],
)
def test_normalize_disposition_text(disposition, custom_input, expected):
    assert (
        normalize_disposition_text(disposition=disposition, custom_input=custom_input)
        == expected
    )


@pytest.mark.parametrize(
    "text",
    [
        SKIPPED,
        CANCELLED,
        "用户已跳过此题，未选择任何选项。",
        "Cancelled. No answer provided.",
        f"Q: {SKIPPED}",
        "header\n用户已取消本次问答，未作答。",
    ],
)
def test_is_skipped_or_cancelled_true(text):
    assert is_skipped_or_cancelled(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "取消",
        "无需修改，进入下一阶段",
        "跳过复审",
        "ordinary revision feedback",
    ],
)
def test_is_skipped_or_cancelled_false(text):
    assert is_skipped_or_cancelled(text) is False


def test_build_answers_dict_disposition_to_sentinel():
    answers = build_ask_user_answers_dict(
        [
            {
                "question": "Q1?",
                "selected_options": [],
                "custom_input": SKIPPED,
                "disposition": "skipped",
            },
            {
                "question": "Q2?",
                "selected_options": ["keep"],
                "custom_input": "",
                "disposition": "cancelled",
            },
        ]
    )
    assert answers == {"Q1?": SKIPPED, "Q2?": CANCELLED}


def test_build_answers_dict_legacy_phrase_to_sentinel():
    answers = build_ask_user_answers_dict(
        [
            {
                "question": "Q?",
                "selected_options": [],
                "custom_input": "Skipped. No option selected.",
            }
        ]
    )
    assert answers == {"Q?": SKIPPED}


def test_build_answers_dict_ordinary_option_unchanged():
    answers = build_ask_user_answers_dict(
        [
            {
                "question": "Q?",
                "selected_options": ["无需修改，进入下一阶段"],
            }
        ]
    )
    assert answers == {"Q?": "无需修改，进入下一阶段"}


def test_build_answers_dict_empty_other_stays_empty():
    """#2330: bare Other without custom text must not invent an answer."""
    answers = build_ask_user_answers_dict(
        [{"question": "Q?", "selected_options": ["Other"], "custom_input": ""}]
    )
    assert answers == {}
