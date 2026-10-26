# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""build_review_prompt 单元测试."""

import pytest

from jiuwenswarm.gateway.message_handler.prompts.review_prompt import build_review_prompt


@pytest.fixture(autouse=True)
def _english_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.prompts.get_config",
        lambda: {"preferred_language": "en"},
    )


def test_build_review_prompt_empty_args_lists_prs_in_english() -> None:
    prompt = build_review_prompt("")
    assert "`gh pr list`" in prompt
    assert "`gh pr view <number>`" in prompt
    assert "`gh pr diff <number>`" in prompt
    assert "PR number: " in prompt
    assert prompt.rstrip().endswith("PR number:")
    assert "Respond in English." in prompt
    assert "git log" not in prompt
    assert "git diff" not in prompt


def test_build_review_prompt_passes_through_args() -> None:
    prompt = build_review_prompt("123 focus on security")
    assert prompt.rstrip().endswith("PR number: 123 focus on security")
    assert "Respond in English." in prompt


def test_build_review_prompt_chinese_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.prompts.get_config",
        lambda: {"preferred_language": "zh"},
    )
    prompt = build_review_prompt("42")
    assert "Respond in Chinese (simplified)." in prompt
    assert prompt.rstrip().endswith("PR number: 42")
