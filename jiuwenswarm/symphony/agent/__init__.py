"""Agent-facing retrieval helpers built on top of the symphony skill index."""

from __future__ import annotations

from .agentic_retrieval_toolkit import (
    AgenticRetrievalToolKit,
    build_skill_index,
    is_agentic_retrieval_enabled,
    render_skill_retrieval_prompt,
    render_skill_retrieval_prompt_for_visible_skills,
    skill_branch_explore,
    skill_branch_explore_for_visible_skills,
    skill_branch_peek,
    skill_branch_peek_for_visible_skills,
)
from .tool_result import AgenticToolResult

__all__ = [
    "AgenticRetrievalToolKit",
    "AgenticToolResult",
    "build_skill_index",
    "is_agentic_retrieval_enabled",
    "render_skill_retrieval_prompt",
    "render_skill_retrieval_prompt_for_visible_skills",
    "skill_branch_explore",
    "skill_branch_explore_for_visible_skills",
    "skill_branch_peek",
    "skill_branch_peek_for_visible_skills",
]
