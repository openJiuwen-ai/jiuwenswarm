# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the auto-memory extraction prompt templates."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.auto_memory.prompts import (
    build_extract_memories_prompt,
)


def test_english_prompt_renders_literal_file_path_placeholder() -> None:
    """The English template must not treat the placeholder as an expression."""
    prompt = build_extract_memories_prompt(3, [], language="en")

    assert "There are 3 new messages" in prompt
    assert "{semantic-name}.md" in prompt


def test_english_prompt_includes_existing_memories() -> None:
    memories = [{"name": "user-role", "description": "Data scientist"}]

    prompt = build_extract_memories_prompt(1, memories, language="en")

    assert "- user-role: Data scientist" in prompt


def test_chinese_prompt_renders_literal_file_path_placeholder() -> None:
    prompt = build_extract_memories_prompt(2, [], language="zh")

    assert "2 条新消息" in prompt
    assert "{语义命名}.md" in prompt
