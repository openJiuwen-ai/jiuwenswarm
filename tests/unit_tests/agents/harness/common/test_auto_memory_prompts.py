# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the Auto Memory extraction prompt builders."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.auto_memory.prompts import (
    build_extract_memories_prompt,
)


def test_english_prompt_keeps_the_file_path_hint_literal() -> None:
    prompt = build_extract_memories_prompt(
        new_messages_count=3,
        existing_memories=[{"name": "user_role", "description": "role notes"}],
        language="en",
    )

    assert "{semantic-name}.md" in prompt
    assert "user_role: role notes" in prompt
    assert "{new_messages_count}" not in prompt
    assert "{existing_summary}" not in prompt


def test_both_languages_render_without_leftover_placeholders() -> None:
    for language in ("en", "zh"):
        prompt = build_extract_memories_prompt(
            new_messages_count=0, existing_memories=[], language=language
        )

        assert "0 new messages" in prompt or "0 条新消息" in prompt
        assert "{new_messages_count}" not in prompt
        assert "{existing_summary}" not in prompt
