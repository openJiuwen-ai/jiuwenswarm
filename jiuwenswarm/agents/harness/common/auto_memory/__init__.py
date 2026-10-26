# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Auto Memory module for jiuwenswarm.

This module implements automatic memory extraction after conversation ends,
similar to Claude Code's auto-memory feature.

Key components:
- extract_memories: Utility functions for scanning memory files, mutex detection, message conversion
- prompts: Prompt templates for memory extraction
- tool_restriction_rail: Rail for restricting tool calls (canUseTool-like)
- extraction_runner: Main extraction logic (subagent functions)
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.auto_memory.extraction_runner import (
    _execute_auto_memory_extraction,
)

__all__ = [
    "_execute_auto_memory_extraction",
]