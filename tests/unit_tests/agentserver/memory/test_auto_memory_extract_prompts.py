# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the auto-memory extraction prompt builders.

``_build_extract_prompt_en`` returns an f-string, so a brace pair in the template
body is an *expression*, not literal text: ``{semantic-name}`` was parsed as
``semantic - name`` and raised ``NameError: name 'semantic' is not defined``
every time an English memory extraction ran.  ``build_extract_memories_prompt``
calls it for every non-``zh`` language, so the failure covered all English
sessions.  These cases pin both the runtime behaviour and the literal text.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.auto_memory.prompts import (
    build_extract_memories_prompt,
)

EXISTING = [{"name": "user_role", "description": "backend engineer"}]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_build_extract_memories_prompt_does_not_raise(language: str) -> None:
    """Building a prompt must not depend on names that only exist in the template."""
    prompt = build_extract_memories_prompt(7, EXISTING, language=language)

    assert isinstance(prompt, str)
    assert prompt
    # The known-files summary must reach the model in both languages.
    assert "user_role" in prompt


def test_english_prompt_keeps_the_memory_file_placeholder_literal() -> None:
    """The placeholder is documentation for the model, not a computed value."""
    prompt = build_extract_memories_prompt(7, [], language="en")

    assert "File path: {semantic-name}.md" in prompt
