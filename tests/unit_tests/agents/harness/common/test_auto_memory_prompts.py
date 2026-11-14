# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""auto_memory 提取 prompt 构建的行为测试（#6125）。

英文 prompt 曾是 f-string 里的裸花括号 ``{semantic-name}``，构建时被当成表达式
``semantic - name`` 求值并抛 ``NameError``；异常被上层吞掉后，英文会话的自动记忆
永不落库。这里钉住两种语言都能正常构建，且占位符仍以字面量出现在 prompt 中。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.auto_memory.prompts import build_extract_memories_prompt


def test_build_extract_prompt_en_keeps_semantic_name_placeholder():
    prompt = build_extract_memories_prompt(
        3,
        [{"name": "user_role", "description": "role"}],
        language="en",
    )
    # 修复前此处直接抛 NameError：name 'semantic' is not defined。
    assert "{semantic-name}.md" in prompt
    assert "There are 3 new messages" in prompt
    assert "user_role: role" in prompt


def test_build_extract_prompt_en_without_existing_memories():
    prompt = build_extract_memories_prompt(0, [], language="en")
    assert "{semantic-name}.md" in prompt
    assert "There are 0 new messages" in prompt


def test_build_extract_prompt_zh_still_builds():
    prompt = build_extract_memories_prompt(
        2,
        [{"name": "user_role", "description": "角色"}],
        language="zh",
    )
    assert "对话中有 2 条新消息" in prompt
    assert "user_role: 角色" in prompt


def test_non_zh_language_falls_back_to_en_prompt():
    """非 zh 语言统一走英文分支，修复必须覆盖这条路径。"""
    prompt = build_extract_memories_prompt(1, [], language="ja")
    assert "{semantic-name}.md" in prompt
